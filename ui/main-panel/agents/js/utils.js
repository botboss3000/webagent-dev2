'use strict';

/**
 * Agents — shared utilities (esc, btn, autosave, debounce, etc.)
 */

import { icon, claudeMark, codexMark } from '../../../shared/js/icons.js';
import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { writeAgentCache } from './agent-cache.js';
import { _esc } from '../../../shared/js/dom-utils.js';
import {
  _agents, _isMockAgent, _patchMockAgentDraft, MOCK_AGENT_ID,
  _binView,
} from './state.js';

// ── Escaping ──────────────────────────────────────────────────────────────────

// HTML escaping is the app-wide canonical _esc (DOM-based, shared/dom-utils.js).
// Re-exported here so existing agents-tab code that imports _esc from this module
// keeps working unchanged. Do not reintroduce a local copy.
export { _esc };

// ── Button factory ────────────────────────────────────────────────────────────

export function _btn(label, cls) {
  const b = document.createElement('button');
  b.className = cls;
  b.textContent = label;
  return b;
}

// ── Display helpers ───────────────────────────────────────────────────────────

export function _iconColor(agent) {
  // Local Claude Code agents always wear the Claude-orange chip (matches their
  // create tile), so the spark mark reads as the real Claude brand everywhere.
  if (agent.engine === 'claude_code') return 'color-claude';
  // Local Codex agents always wear the neutral Codex-steel chip (matches their
  // create tile), so the knot mark reads as the real Codex brand everywhere.
  if (agent.engine === 'codex') return 'color-codex';
  // Terminal Chat agents wear the terminal-green chip.
  if (agent.engine === 'terminal_chat') return 'color-terminal';
  if (agent.access_level === 'admin_only') return 'color-red';
  const id = (agent.id || '').toLowerCase();
  if (id.includes('planner') || id.includes('closer') || id.includes('opt')) return 'color-purple';
  if (agent.source === 'custom') return 'color-blue';
  return 'color-teal';
}

export function _timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60)    return 'just now';
  if (diff < 3600)  return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

export function _displayName(agent) {
  return agent.name || (agent.source === 'custom' ? 'Fallback Name' : agent.id);
}

export function _triggerKeyPlaceholder(triggerType) {
  const map = {
    slash_command: 'Slash command (e.g. /optimize)',
    tool_call:     'Tool name (e.g. run_optimizer)',
    schedule:      'Cron expression (e.g. 0 9 * * *)',
    webhook:       'Webhook path slug',
    background:    'Internal identifier',
  };
  return map[triggerType] || '';
}

// ── Agent icon renderer ──────────────────────────────────────────────────────

// The canonical icon list now lives in the shared icon picker; alias it here
// under the legacy name so existing agents code (icon validation, rendering)
// keeps working unchanged. Imported (not just re-exported) so it is also a usable
// local binding inside this module (e.g. _renderAgentIcon below).
import { ICON_PICKER_ICONS } from '../../../shared/js/icon-picker.js';
const _ICON_PICKER_ICONS = ICON_PICKER_ICONS;

export function _renderAgentIcon(agent, size) {
  const name = agent.icon || '';
  // Every agent icon renders ~1.5× the requested size so the glyph fills its chip
  // boldly. Introduced for the Claude spark; now applied to ALL agents so they
  // match. Covers the Claude mark, Lucide icons, the bot fallback and emoji/text.
  const n = parseFloat(size) || 24;
  const unit = String(size).replace(/[\d.]/g, '') || 'px';
  const big = `${n * 1.5}${unit}`;
  // Local Claude Code agents wear the real Claude spark mark by default — unless an
  // admin has picked a different icon for this one (then respect that choice).
  if (agent.engine === 'claude_code' && (!name || name === 'sparkles')) {
    return claudeMark({ size: big });
  }
  // Local Codex agents wear the real Codex knot mark by default (its template
  // icon is 'code-2') — unless an admin has picked a different icon.
  if (agent.engine === 'codex' && (!name || name === 'code-2')) {
    return codexMark({ size: big });
  }
  // Terminal Chat agents wear the terminal icon by default.
  if (agent.engine === 'terminal_chat' && (!name || name === 'terminal')) {
    return icon('terminal', { size: big });
  }
  if (!name) return icon('bot', { size: big });
  if (_ICON_PICKER_ICONS.includes(name)) return icon(name, { size: big });
  // Emoji / raw-text icon: scale via font-size so it matches the sized glyphs above.
  return `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${_esc(name)}</span>`;
}

// ── Auto-save helpers ─────────────────────────────────────────────────────────

export function _makeAutosaveCheck() {
  const el = document.createElement('span');
  el.className = 'agents-autosave-check';
  el.setAttribute('aria-hidden', 'true');
  return el;
}

export function _flashSaved(indicator, ok = true, errMsg = '') {
  if (!indicator) return;
  clearTimeout(indicator._fadeT);
  indicator.classList.remove('saving', 'saved', 'error');
  if (ok) {
    indicator.classList.add('saved');
    indicator.textContent = '✓';
    indicator.title = 'Saved';
    indicator._fadeT = setTimeout(() => {
      indicator.classList.remove('saved');
      indicator.textContent = '';
    }, 2200);
  } else {
    indicator.classList.add('error');
    indicator.textContent = '!';
    indicator.title = errMsg || 'Save failed';
    indicator._fadeT = setTimeout(() => {
      indicator.classList.remove('error');
      indicator.textContent = '';
    }, 4000);
  }
}

// fallow-ignore-next-line unused-export
export function _markSaving(indicator) {
  if (!indicator) return;
  clearTimeout(indicator._fadeT);
  indicator.classList.remove('saved', 'error', 'saving');
  indicator.textContent = '';
}

/** Debounce a handler so rapid typing only saves once it settles. */
export function _debounced(fn, ms = 700) {
  let t;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.flush = (...args) => { clearTimeout(t); fn(...args); };
  return wrapped;
}

/**
 * PUT a partial update for a single custom agent. Returns true on success.
 * Flashes `indicator` (if given) and patches the local agent cache.
 *
 * opts.silent — skip the visual surgical re-render (square pulse + open-card
 * rebuild) after the save. The local caches are still patched, so data stays in
 * sync; only the disruptive DOM rebuild is suppressed. Use it for controls that
 * already reflect their own change IN PLACE (e.g. the model table), so saving a
 * single toggle doesn't tear down and re-mount the whole agent card underneath
 * the user. The default (no opts) keeps the original re-render behaviour.
 *
 * opts.returnAgent — return the authoritative persisted agent object on success
 * instead of `true`. This is for editors which must verify server-normalized data
 * before showing a success state; existing boolean callers are unchanged.
 */
export async function _putAgentField(agent, updates, indicator, opts = {}) {
  if (!agent || (agent.source !== 'custom' && agent.id !== 'shared_default')) return false;
  if (_isMockAgent(agent)) {
    Object.assign(agent, _patchMockAgentDraft(updates));
    _flashSaved(indicator, true);
    return opts.returnAgent ? agent : true;
  }
  _markSaving(indicator);
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId, ...updates }),
    });
    const data = await res.json();
    if (res.ok) {
      const idx = _agents.findIndex(a => a.id === agent.id);
      if (idx !== -1) Object.assign(_agents[idx], data.agent);
      Object.assign(agent, data.agent);
      // Keep the chat side-panel's cached agent list in sync (it carries the
      // per-agent chat_ui the composer/welcome bubbles read) so edits show in a
      // new chat session without a full reload.
      try {
        const shared = window.__agentsSharedData && window.__agentsSharedData.agents;
        if (Array.isArray(shared)) {
          const si = shared.findIndex(a => a && a.id === agent.id);
          if (si !== -1) Object.assign(shared[si], data.agent);
        }
      } catch (_) { /* non-fatal */ }
      writeAgentCache('list:main', window.__agentsSharedData || { agents: _agents }, 5 * 60 * 1000);
      // Notify surgical update — unless the caller asked to stay silent (the
      // control reflects its own change in place and a card rebuild would be
      // disruptive). Caches above are already patched either way.
      if (!opts.silent && typeof window.__agentsSurgicalUpdateSquare === 'function') {
        window.__agentsSurgicalUpdateSquare(agent);
      }
      _flashSaved(indicator, true);
      return opts.returnAgent ? data.agent : true;
    }
    _flashSaved(indicator, false, data.detail || 'Save failed');
    return false;
  } catch (e) {
    _flashSaved(indicator, false, e.message);
    return false;
  }
}

/**
 * Non-optimistic toggle save. Shows a spinner while the request is in flight.
 */
export async function _toggleSave(agent, control, currentOn, buildUpdates, applyState, indicator) {
  if (control.dataset.busy === '1') return;
  const targetOn = !currentOn;
  control.dataset.busy = '1';
  control.classList.add('control-saving');
  const prevHTML = control.innerHTML;
  control.innerHTML = '<span class="agents-spinner"></span>';
  _markSaving(indicator);
  const ok = await _putAgentField(agent, buildUpdates(targetOn), indicator);
  control.dataset.busy = '0';
  control.classList.remove('control-saving');
  if (ok) {
    applyState(targetOn);
  } else {
    control.innerHTML = prevHTML;
  }
}

// ── Sort agents ───────────────────────────────────────────────────────────────

'use strict';

/**
 * Agents — mock (create-in-place) agent: shared create state + the create POST.
 *
 * There is ONE "New Agent" tile. Clicking it opens a single create card whose
 * *type* is chosen by a segmented toggle — WebAgent / Claude / Terminal (built in
 * ui/main-panel/agents/js/view.js → _buildMockTypeToggle). This module owns the
 * cross-render create state (the chosen type, the in-progress name, and the
 * per-engine draft settings) so a card re-render (segment switch / tab switch)
 * never loses what you typed, plus `_postNewAgent`, the POST that turns the form
 * into a real agent.
 *
 * The card chrome + the per-type lower area are built in view.js; the Claude /
 * Terminal draft forms live in their sibling modules (claude-agent.js /
 * terminal-chat-agent.js, exported as renderClaudeCreateBody /
 * renderTerminalChatCreateBody). The accept ("+") dispatch is in view.js
 * (_acceptMockCreate), which has all three modules in scope.
 */

import { app } from '../../../shared/js/state.js';
import {
  MOCK_AGENT_ID, _expandedAgents, _setActive, _createMockAgent,
  _patchMockAgentDraft, _resetMockAgentDraft,
} from './state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { invalidateAgentListCaches } from './agent-cache.js';
import { ICON_PICKER_ICONS } from '../../../shared/js/icon-picker.js';

// Reuse the agent avatar picker's complete canonical catalog. One choice stays
// stable for this draft, then becomes the persisted avatar on successful create.
const _MOCK_ICON_CHOICES = ICON_PICKER_ICONS;
let _mockIconName = null;

export function _mockRandomIcon() {
  if (!_mockIconName) _mockIconName = _MOCK_ICON_CHOICES[Math.floor(Math.random() * _MOCK_ICON_CHOICES.length)];
  return _mockIconName;
}

function _resetMockIcon() { _mockIconName = null; }

// ── Cross-render create state ──────────────────────────────────────────────────
// Held in module scope so a card re-render (type switch, tab switch) keeps the
// in-progress form. Reset only after a successful create (_resetMockCreateState).
let _createType = 'webagent';                    // 'webagent' | 'claude' | 'codex' | 'terminal'
let _draftName  = '';                            // the typed agent name
let _engineDraft = { claude: {}, codex: {}, terminal: {} }; // per-engine draft settings
// Draft ability selections deliberately stay client-side until the agent exists:
// connection rows require a persisted agent id on the API.
let _draftAbilityConnections = new Map();

export function _mockCreateType() { return _createType; }
export function _setMockCreateType(t) { _createType = t; }
export function _mockDraftName() { return _draftName || _createMockAgent().name || ''; }
export function _mockEngineDraft() { return _engineDraft; }
export function _mockAbilityConnections() {
  return [..._draftAbilityConnections.entries()].map(([connection_type, value]) => ({
    connection_type,
    enabled: !!value.enabled,
    config: structuredClone(value.config || {}),
  }));
}
export function _saveMockAbilityConnection(connectionType, enabled, config = {}) {
  _draftAbilityConnections.set(connectionType, { enabled: !!enabled, config: structuredClone(config || {}) });
}

function _resetMockCreateState() {
  _createType = 'webagent';
  _draftName = '';
  _engineDraft = { claude: {}, codex: {}, terminal: {} };
  _draftAbilityConnections = new Map();
  _resetMockAgentDraft();
}

// ── Header name field + accept wiring ───────────────────────────────────────────
// The contenteditable name input and the "+" accept button. `onAccept` is supplied
// by view.js and dispatches on the chosen type; the name field seeds from / writes
// to the persisted draft so it survives re-renders.
export function _wireMockCreateField(card, onAccept) {
  const field = card.querySelector('.agent-mock-create-field');
  if (!field) return;
  const input = field.querySelector('.agent-mock-name-input');
  if (!input) return;
  field.addEventListener('click', e => e.stopPropagation());

  if (_draftName && !(input.textContent || '').trim()) input.textContent = _draftName;
  input.addEventListener('input', () => {
    _draftName = (input.textContent || '').trim();
    _patchMockAgentDraft({ name: _draftName });
  });

  const accept = (typeof onAccept === 'function') ? onAccept : () => {};
  const createGo = card.querySelector('.agent-mock-create-go');
  if (createGo) createGo.addEventListener('click', (e) => { e.stopPropagation(); accept(); });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); accept(); }
    else if (e.key === 'Escape') { input.blur(); }
  });
  input.addEventListener('paste', (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData('text').replace(/\s+/g, ' ').trim();
    document.execCommand('insertText', false, text);
  });
}

// ── Create POST ─────────────────────────────────────────────────────────────────
// fallow-ignore-next-line unused-export
export async function _postNewAgent({ name, description = '', templateId = 'default', config = {} }) {
  // The create API applies metadata updates after cloning its template. Keep the
  // submitted value for the response object too, so the card never flashes the
  // template's fallback icon while the roster cache refreshes.
  const assignedIcon = config.icon || _mockRandomIcon();
  const res = await fetch('/api/v1/agents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      ...config,
      user_id: app.currentUserId,
      name,
      description,
      // Persist the random avatar selected for this create draft. An explicit
      // caller-provided icon remains authoritative for programmatic creation.
      icon: assignedIcon,
      template_id: templateId || 'default',
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.detail || 'Failed to create agent');
    return null;
  }
  const newAgent = data.agent;
  if (!newAgent) return null;
  newAgent.icon = assignedIcon;

  _expandedAgents.delete(MOCK_AGENT_ID);
  _resetMockIcon();
  _resetMockCreateState();

  window.__agentsSharedData = null;
  invalidateAgentListCaches();

  _setActive(newAgent.id, 'config');

  // Add surgically via the grid module (registered callback pattern)
  if (typeof window.__agentsSurgicalAddSquare === 'function') {
    window.__agentsSurgicalAddSquare(newAgent);
  }
  if (typeof window.__agentsRebuildDetailRegion === 'function') {
    window.__agentsRebuildDetailRegion();
  }

  app.currentAgentId = newAgent.id;
  try { localStorage.setItem('selectedAgentId', newAgent.id); } catch (_) {}
  if (typeof app.populateAgentSelect === 'function') {
    try { await app.populateAgentSelect(app.currentUserId); } catch (_) {}
  }
  return newAgent;
}

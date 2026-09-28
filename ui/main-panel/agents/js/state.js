'use strict';

/**
 * Agents — shared state and constants.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { readAgentCache, writeAgentCache } from './agent-cache.js';

// ── State ─────────────────────────────────────────────────────────────────────
export let _agents         = [];   // full list from server
export let _expandedAgents = new Map(); // Map<agentId, { tab: string }>
export let _userIsAdmin    = false;
export let _extendLlmToAgents = true; // mirrors app-settings.json extend_llm_to_agents
export let _showSystem     = false;
export let _binView        = false;
export let _clonesView     = false;
export let _selectedIds    = new Set();

// The id of the single open agent, or null. (First/only key in _expandedAgents.)
export function _activeId() {
  for (const id of _expandedAgents.keys()) return id;
  return null;
}

// ── Live run-status tracking ──────────────────────────────────────────────────
// Which agents are currently running a turn, driven by `agent_status` WS events
// (run start/stop). Keyed by agentId → Set of running session ids, so an agent
// with several concurrent sessions only goes idle once its LAST run finishes.
const _runningSessionsByAgent = new Map();

export function _isAgentRunning(agentId) {
  const s = _runningSessionsByAgent.get(agentId);
  return !!(s && s.size);
}

// Record a run start/stop. Returns true when the agent's overall running state
// flipped (so the caller knows whether the status dot needs repainting).
export function _setAgentRunning(agentId, sessionId, running) {
  if (!agentId) return false;
  const before = _isAgentRunning(agentId);
  let set = _runningSessionsByAgent.get(agentId);
  if (running) {
    if (!set) { set = new Set(); _runningSessionsByAgent.set(agentId, set); }
    set.add(sessionId || agentId);
  } else if (set) {
    set.delete(sessionId || agentId);
    if (!set.size) _runningSessionsByAgent.delete(agentId);
  }
  return _isAgentRunning(agentId) !== before;
}

// Open exactly one agent (clearing any other), defaulting to protected Config.
// Config remains the full launcher when a user has no other favorite tabs.
export function _setActive(agentId, tab = 'model') {
  _expandedAgents.clear();
  _expandedAgents.set(agentId, { tab, configPage: '' });
}

/** Nested Agent Config page state is intentionally local to the expanded agent.
 * It survives ordinary top-level tab rerenders but cannot leak to another agent. */
export function _getAgentConfigPage(agentId) {
  return _expandedAgents.get(agentId)?.configPage || '';
}

export function _setAgentConfigPage(agentId, pageId = '') {
  const state = _expandedAgents.get(agentId);
  if (!state) return;
  state.configPage = pageId || '';
}

// ── Mock agent (create-in-place) ─────────────────────────────────────────────
export const MOCK_AGENT_ID = '__new__';

const _MOCK_AGENT_DEFAULTS = Object.freeze({
  id: MOCK_AGENT_ID,
  name: '',
  description: '',
  source: 'custom',
  icon: null,
  access_level: 'user',
  llm_config: { use_default: true },
  chat_ui: {},
  embed: {},
  safety_policy: {},
  allowed_tools: null,
  loop_logic: null,
  trigger_type: 'user_input',
  trigger_key: null,
  default_execution_mode: 'ask',
  default_target_device: '',
  public_link: false,
  resume_tail_messages: null,
  resume_max_attempts: null,
  resume_backoff_seconds: null,
  closing_max_attempts: null,
  closing_max_attempts_per_requirement: null,
  assertions_max_attempts: null,
  max_turn_count: 12,
  max_wall_seconds: null,
  max_tokens: 8096,
  max_identical_tool_calls: 0,
  max_stall_strikes: 0,
  user_mode: 'anonymous',
  slots: [],
  discoverable: false,
  is_mock: true,
});

function _freshMockAgent() {
  return structuredClone(_MOCK_AGENT_DEFAULTS);
}

let _mockAgentDraft = _freshMockAgent();
let _mockAgentDirtyFields = new Set();

export function _createMockAgent() {
  return _mockAgentDraft;
}

const _MOCK_DEEP_FIELDS = new Set(['llm_config', 'chat_ui', 'embed', 'safety_policy']);

export function _patchMockAgentDraft(updates = {}) {
  Object.entries(updates || {}).forEach(([key, value]) => {
    _mockAgentDirtyFields.add(key);
    if (_MOCK_DEEP_FIELDS.has(key) && value && typeof value === 'object' && !Array.isArray(value)) {
      const current = (_mockAgentDraft[key] && typeof _mockAgentDraft[key] === 'object')
        ? _mockAgentDraft[key] : {};
      _mockAgentDraft[key] = { ...current, ...value };
    } else {
      _mockAgentDraft[key] = value;
    }
  });
  return _mockAgentDraft;
}

export function _resetMockAgentDraft() {
  _mockAgentDraft = _freshMockAgent();
  _mockAgentDirtyFields = new Set();
  return _mockAgentDraft;
}

export function _mockAgentConfigPayload() {
  const excluded = new Set([
    'id', 'name', 'source', 'access_level', 'is_mock', 'discoverable', 'slots',
    'template_id', 'template_origin', 'template_source', 'template_has_json',
  ]);
  return Object.fromEntries(
    Object.entries(_mockAgentDraft)
      .filter(([key, value]) => _mockAgentDirtyFields.has(key)
        && !excluded.has(key) && value !== undefined)
      .map(([key, value]) => [key, structuredClone(value)]),
  );
}

export function _isMockAgent(agent) {
  return agent && agent.id === MOCK_AGENT_ID;
}

// ── Tool tier definitions ─────────────────────────────────────────────
// Tier 1: Always-on. Present for all agents, not shown as toggleable.
const TIER_1_ALWAYS_ON = new Set([
  'list_tools','search_tools','get_tool_definition',
  'get_time','get_date','calculate','read_attachment',
  'register_user',
]);

// Tier 2: Configurable standard tools — shown in the toggle UI.
const TIER_2_TOOLS = [
  'web_search','http_request','browser_action',
  'db_query','list_agent_context_documents','get_agent_context_document',
  'update_agent_context_document','insert_agent_context_document','session_search',
  'memory',
  'get_weather','create_tool','rate_skill','run_optimizer',
  'register_webhook','list_webhooks','delete_webhook','get_webhook_log',
];

const TIER_2_ALL = TIER_2_TOOLS;

const PIPELINE_TOOLS = {
  opt_planner: ['run_worker_trials','handoff_to_closer'],
  opt_closer:  ['deploy_optimization'],
};

// ── Tier 2 tool categories (for the loop diagram / Tools tab) ─────────────────
export const TIER_2_CATEGORIES = [
  { label: 'Big-code', tools: [
    { name: 'read_source', destructive: false, desc: 'Read any file in the workspace' },
    { name: 'edit_source', destructive: true, desc: 'Edit file in workspace (requires confirmation)' },
    { name: 'write_source', destructive: true, desc: 'Create new files in workspace' },
    { name: 'patch_source', destructive: false, desc: 'Apply targeted diffs to files' },
    { name: 'delete_source', destructive: true, desc: 'Delete files from workspace' },
    { name: 'search_source', destructive: false, desc: 'Grep / rg across workspace files' },
  ]},
  { label: 'Shell', tools: [
    { name: 'run_command', destructive: true, desc: 'Execute a shell command on the host' },
    { name: 'restart_server', destructive: true, desc: 'Restart the web app server' },
    { name: 'resolve_conflict', destructive: false, desc: 'Resolve merge conflict in a file' },
    { name: 'terminal_open', destructive: false, desc: 'Open an interactive shell terminal' },
  ]},
  { label: 'Git', tools: [
    { name: 'git_tool', destructive: false, desc: 'Status, diff, commit, push, branch, merge, log' },
  ]},
  { label: 'Browser', tools: [
    { name: 'browser_action', destructive: false, desc: 'Drive a headless browser' },
    { name: 'http_request', destructive: false, desc: 'Make arbitrary HTTP/s requests' },
  ]},
  { label: 'Web', tools: [
    { name: 'web_search', destructive: false, desc: 'Search the web via DuckDuckGo' },
    { name: 'get_weather', destructive: false, desc: 'Weather conditions by city/zip' },
    { name: 'maps_geocode', destructive: false, desc: 'Geocode addresses with location mapping' },
  ]},
  { label: 'DB / Data', tools: [
    { name: 'db_query', destructive: false, desc: 'Query local SQLite DB' },
    { name: 'get_agent_context_document', destructive: false, desc: 'Read context document' },
    { name: 'list_agent_context_documents', destructive: false, desc: 'List context documents' },
    { name: 'insert_agent_context_document', destructive: false, desc: 'Add a context document' },
    { name: 'update_agent_context_document', destructive: false, desc: 'Edit a context document' },
    { name: 'session_search', destructive: false, desc: 'Search past session transcripts' },
  ]},
  { label: 'Memory & Skills', tools: [
    { name: 'memory', destructive: false, desc: 'Read/write brain memory store' },
    { name: 'create_tool', destructive: false, desc: 'Register a new tool / skill' },
    { name: 'rate_skill', destructive: false, desc: 'Rate / give feedback on a skill' },
    { name: 'run_optimizer', destructive: false, desc: 'Run the optimizer on this session' },
  ]},
  { label: 'Self-improvement', tools: [
    { name: 'read_own_prompt', destructive: false, desc: 'Read this agent\'s own prompt sections' },
    { name: 'edit_own_prompt', destructive: true, desc: 'Improve this agent\'s own unlocked prompt sections' },
    { name: 'save_own_skill', destructive: true, desc: 'Teach itself a reusable how-to skill' },
    { name: 'remove_own_skill', destructive: true, desc: 'Delete one of its own skills' },
  ]},
  { label: 'Webhooks', tools: [
    { name: 'register_webhook', destructive: false, desc: 'Register a new webhook' },
    { name: 'list_webhooks', destructive: false, desc: 'List registered webhooks' },
    { name: 'delete_webhook', destructive: false, desc: 'Delete a webhook' },
    { name: 'get_webhook_log', destructive: false, desc: 'Get webhook execution log' },
  ]},
  { label: 'Agent Management', tools: [
    { name: 'delegate_to_agent', destructive: false, desc: 'Hand off session to another agent' },
    { name: 'list_delegatable_agents', destructive: false, desc: 'List agents available for handoff' },
    { name: 'list_agent_templates', destructive: false, desc: 'List agent template catalog' },
    { name: 'list_my_agents', destructive: false, desc: 'List agents available to you' },
    { name: 'get_agent', destructive: false, desc: 'Get an agent\'s full definition' },
    { name: 'create_agent', destructive: false, desc: 'Create a new agent' },
    { name: 'update_agent', destructive: false, desc: 'Update an agent\'s definition' },
    { name: 'edit_agent_prompt', destructive: false, desc: 'Edit agent prompt sections' },
    { name: 'set_agent_ability', destructive: false, desc: 'Toggle agent abilities' },
  ]},
  { label: 'Wiki', tools: [
    { name: 'wiki_search', destructive: false, desc: 'Search the Wiki' },
    { name: 'wiki_get', destructive: false, desc: 'Get a Wiki page' },
    { name: 'wiki_list', destructive: false, desc: 'List Wiki pages' },
    { name: 'wiki_create', destructive: false, desc: 'Create a Wiki page' },
    { name: 'wiki_update', destructive: false, desc: 'Update a Wiki page' },
    { name: 'wiki_delete', destructive: false, desc: 'Delete a Wiki page' },
    { name: 'wiki_history', destructive: false, desc: 'View Wiki revision history' },
    { name: 'wiki_get_revision', destructive: false, desc: 'Get a specific Wiki revision' },
    { name: 'wiki_restore', destructive: false, desc: 'Restore to a previous Wiki revision' },
    { name: 'wiki_backlinks', destructive: false, desc: 'Find Wiki pages linking to a page' },
  ]},
  { label: 'Image', tools: [
    { name: 'generate_image', destructive: false, desc: 'Generate images via a provider/model' },
  ]},
  { label: 'Diagnostics', tools: [
    { name: 'read_diagnostics', destructive: false, desc: 'Read in-app diagnostics (flight recorder)' },
  ]},
  { label: 'Agent orchestration', tools: [
    { name: 'delegate_to_agent', destructive: false, desc: 'Hand off session to another agent' },
    { name: 'list_delegatable_agents', destructive: false, desc: 'List agents available for handoff' },
  ]},
  { label: 'Automation', tools: [
    { name: 'automation_schedule', destructive: false, desc: 'Schedule recurring automated tasks' },
    { name: 'automation_trigger', destructive: false, desc: 'Configure event-driven triggers' },
  ]},
  { label: 'Integration', tools: [
    { name: 'integration_send', destructive: false, desc: 'Send via an OAuth integration' },
  ]},
];

// ── Ability-to-tools mapping ──────────────────────────────────────────
// Maps agent connection_type (ability) to the tool names it unlocks. Used only
// for the agent-card tool-count badge.
// FALLBACK ONLY — overwritten at runtime by the fetched ability catalog
// (GET /api/v1/abilities/catalog → _ensureAbilityCatalog). Do NOT add a new
// ability here; drop a file in plugins/abilities/ instead.
export let ABILITY_TO_TOOLS = {
  codebase_admin:   ['read_source','write_source','edit_source','delete_source','run_command','restart_server','db_query'],
  git_control:      ['git_tool','resolve_conflict','commit_and_push'],
  ui_admin:         ['read_source','write_source','edit_source','patch_source','delete_source','search_source','read_directory'],
  web_access:       ['web_search','get_weather','maps_geocode'],
  browser_control:  ['browser_action','http_request'],
  terminal_control: ['terminal_open','terminal_read','terminal_send','terminal_wait','terminal_list','terminal_close'],
  image_generation: ['generate_image'],
  create_tools:     ['create_tool'],
  diagnostics:      ['read_diagnostics'],
  automation:       [],  // automation tools are injected dynamically; no static tool names to list
  visualizer:       [],  // visualizer tools are injected dynamically
  agent_orchestration: ['delegate_to_agent','list_delegatable_agents'],
  agent_management: ['list_agent_templates','list_my_agents','get_agent','create_agent','update_agent','edit_agent_prompt','set_agent_ability'],
  wiki_control:     ['wiki_search','wiki_list','wiki_get','wiki_create','wiki_update','wiki_delete','wiki_history','wiki_get_revision','wiki_restore','wiki_backlinks'],
  // register_user is always-on (Tier 1), not gated by an ability
};

// Connection icons and notes (shared across abilities/connections tab)
export const _CONN_ICONS = {
  telegram:  'send',
  twilio:    'phone',
  email:     'mail',
  whatsapp:  'message-circle',
  discord:   'gamepad-2',
  slack:     'briefcase',
  google:    'search',
  microsoft: 'monitor',
  yahoo:     'mail',
  dropbox:   'folder-open',
  notion:    'file-text',
  airtable:  'table',
  google_sheets: 'sheet',
  github:    'github',
  gitlab:    'git-branch',
  jira:      'kanban-square',
  bank:      'landmark',
  search:    'globe',
  stripe:    'credit-card',
  paypal:    'wallet',
  square:    'square',
  hubspot:   'contact',
  salesforce:'cloud',
  mailchimp: 'mail',
  scraper:         'globe',
  facebook:  'users',
  instagram: 'camera',
  twitter:   'twitter',
  linkedin:  'linkedin',
  tiktok:    'music',
  pinterest: 'image',
  reddit:    'message-square',
  snapchat:  'zap',
  twitch:    'tv',
  codebase_admin:   'folder-cog',
  git_control:      'git-branch',
  ui_admin:         'paintbrush',
  create_tools:     'wrench',
  automation:       'clock',
  web_access:       'globe',
  browser_control:  'mouse-pointer-2',
  terminal_control: 'terminal',
  image_generation: 'image',
  visualizer:       'layout-dashboard',
  agent_orchestration: 'workflow',
  diagnostics:      'stethoscope',
  agent_management: 'user-cog',
  wiki_control:     'book-open',
};

const _CONN_NOTES = {
  codebase_admin:   'Read, write, edit, delete files and run shell commands on the host. No credentials.',
  git_control:      'Version control — status, diff, commit, push, branch, pull — without shell access.',
  ui_admin:         'Edits only front-end files (ui/ — CSS & HTML); never backend code or the shell.',
  create_tools:     'Lets the agent define new tools in the database. No credentials.',
  web_access:       'Search the web, look up weather, and geocode addresses. No API keys needed.',
  browser_control:  'Drive a headless Chromium browser and make HTTP requests. No credentials.',
  terminal_control: 'Open and drive interactive terminal programs (REPLs, CLIs) — effectively shell access.',
  image_generation: 'Generate images from text prompts. Requires a provider + model config.',
  automation:       'Scheduled tasks and event-triggered jobs that can call integrations.',
  visualizer:       'Create, edit, rename, and delete genui in the Gen UI workspace.',
  agent_orchestration: 'Delegate the session to other agents and run the prompt optimizer.',
  diagnostics:      'Read the in-app flight recorder to diagnose the running app.',
  agent_management: 'List, create, and update agents and edit their prompts and abilities.',
  wiki_control:     'Read, search, create, edit, and delete entries in the company-wide Wiki.',
  telegram:  'Send and receive messages through a Telegram bot.',
  twilio:    'Send and receive SMS / voice via Twilio.',
  email:     'Send and receive email on behalf of the agent.',
  whatsapp:  'Send and receive messages through WhatsApp.',
  discord:   'Send and receive messages through a Discord bot.',
  slack:     'Send and receive messages through Slack.',
};

// Fetch the server-built ability catalog ONCE and overwrite the fallback
let _abilityCatalogPromise = null;
function _applyAbilityCatalog(cat) {
  if (!cat || !Array.isArray(cat.groups) || !cat.groups.length) return false;
  const tools = {};
  for (const [id, a] of Object.entries(cat.abilities || {})) {
    tools[id] = (a.tools || []).slice();
    if (a.icon) _CONN_ICONS[id] = a.icon;
    if (a.description) _CONN_NOTES[id] = a.description;
  }
  ABILITY_TO_TOOLS = tools;
  return true;
}
export function _ensureAbilityCatalog() {
  if (_abilityCatalogPromise) return _abilityCatalogPromise;
  _applyAbilityCatalog(readAgentCache('ability-catalog'));
  _abilityCatalogPromise = (async () => {
    try {
      const res = await fetch('/api/v1/abilities/catalog', { headers: { ...authHeaders() } });
      if (!res.ok) return;
      const cat = await res.json();
      if (_applyAbilityCatalog(cat)) {
        writeAgentCache('ability-catalog', cat, 30 * 60 * 1000);
      }
    } catch (e) {
      console.warn('Ability catalog fetch failed; using built-in fallback', e);
    }
  })();
  return _abilityCatalogPromise;
}

export function _toolsForAgent(agent, enabledAbilities) {
  const id = agent.id || '';
  if (agent.is_admin_agent) {
    return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL,
            'read_source','write_source','edit_source','delete_source','run_command','restart_server','git_tool'];
  }
  if (id === 'opt_planner') return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL, ...(PIPELINE_TOOLS.opt_planner || [])];
  if (id === 'opt_closer')  return [...TIER_1_ALWAYS_ON, ...TIER_2_ALL, ...(PIPELINE_TOOLS.opt_closer  || [])];

  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const base = TIER_2_ALL.filter(name => !disabled.has(name));

  const abilityTools = [];
  if (enabledAbilities) {
    for (const [ability, tools] of Object.entries(ABILITY_TO_TOOLS)) {
      if (enabledAbilities.has(ability)) {
        for (const t of tools) {
          if (!disabled.has(t) && !abilityTools.includes(t)) {
            abilityTools.push(t);
          }
        }
      }
    }
  }

  return [
    ...TIER_1_ALWAYS_ON,
    ...base,
    ...abilityTools,
  ];
}

/** Returns the current loop_logic for the agent (object-array), inc. pending. */
function _resolveLoopLogicObjects(agent, pendingChanges) {
  const pending = Array.isArray(pendingChanges?.loop_logic) ? pendingChanges.loop_logic : null;
  const source  = pending || (Array.isArray(agent.loop_logic) ? agent.loop_logic : []);
  if (source.length === 0 || typeof source[0] === 'string') {
    return [{ node: 'memory_search', enabled: true }, { node: 'memory_save', enabled: true }];
  }
  return source.map(item => ({ ...item }));
}

export function _loopNodeEnabledPersisted(agent, nodeId) {
  const ll = Array.isArray(agent.loop_logic) ? agent.loop_logic : [];
  if (!ll.length || typeof ll[0] === 'string') return true;
  const found = ll.find(it => it && it.node === nodeId);
  return found ? found.enabled !== false : true;
}

export function _memoryStateFromAgent(agent) {
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  return {
    recall: _loopNodeEnabledPersisted(agent, 'memory_search'),
    save: _loopNodeEnabledPersisted(agent, 'memory_save') && !disabled.has('memory_save'),
  };
}

export function _memoryUpdatesFor(agent, recall, save) {
  const ll = Array.isArray(agent.loop_logic) ? agent.loop_logic : [];
  let objs;
  if (!ll.length || typeof ll[0] === 'string') {
    objs = [{ node: 'memory_search', enabled: true }, { node: 'memory_save', enabled: true }];
  } else {
    objs = ll.map(it => ({ ...it }));
  }
  const setNode = (nodeId, enabled) => {
    const f = objs.find(o => o && o.node === nodeId);
    if (f) f.enabled = enabled; else objs.push({ node: nodeId, enabled });
  };
  setNode('memory_search', recall);
  setNode('memory_save', save);

  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  if (recall) disabled.delete('memory'); else disabled.add('memory');
  if (save) disabled.delete('memory_save'); else disabled.add('memory_save');

  return { loop_logic: objs, allowed_tools: [...disabled] };
}

export function _localLoopLogicObjs(agent) {
  const ll = Array.isArray(agent.loop_logic) ? agent.loop_logic : [];
  if (!ll.length || typeof ll[0] === 'string') return [];
  return ll.map(item => ({ ...item }));
}

// Re-export state setters for modules to update state
// _setShowSystem drives the header show/hide-system-agents eye toggle (next to
// the trash button). Exported so view.js can flip it.
export function _setShowSystem(v) { _showSystem = v; }
export function _setBinView(v) { _binView = v; _clonesView = false; }
export function _setClonesView(v) { _clonesView = v; _binView = false; }
function _setSelectedIds(s) { _selectedIds = s; }
export function _setAgents(a) { _agents = a; }
export function _setUserIsAdmin(v) { _userIsAdmin = v; }
export function _setExtendLlmToAgents(v) { _extendLlmToAgents = v; }
export function _clearExpanded() { _expandedAgents.clear(); }
export function _deleteExpanded(id) { _expandedAgents.delete(id); }

// ── Loop diagram panel state ──────────────────────────────────────────────────
// Shared across state.js (which owns the mutators) and tab-agent-loop.js (which
// owns the panel rendering). The pending-changes map accumulates field edits
// while a panel is open, then gets flushed on Save.

export let _lvPendingChanges = {};
let _lvSaveBtnEl = null;

// Update the save button visual when pending changes accumulate.
// Called by _lvSetPending. Defined here so it can access _lvSaveBtnEl.
function _lvMarkDirty() {
  if (!_lvSaveBtnEl) return;
  const n = Object.keys(_lvPendingChanges).length;
  _lvSaveBtnEl.classList.toggle('dirty', n > 0);
  _lvSaveBtnEl.textContent = n > 0 ? `Save (${n} field${n > 1 ? 's' : ''})` : 'Save changes';
}

// Queue a pending change for the loop-diagram save operation.
export function _lvSetPending(key, value) {
  _lvPendingChanges[key] = value;
  _lvMarkDirty();
}

// Enable/disable a node in loop_logic and queue the change.
export function _setNodeLoopEnabled(agent, nodeId, enabled) {
  const objs = _resolveLoopLogicObjects(agent, _lvPendingChanges);
  const idx = objs.findIndex(o => o.node === nodeId);
  if (idx !== -1) {
    objs[idx] = { ...objs[idx], enabled };
  } else {
    objs.push({ node: nodeId, enabled });
  }
  _lvSetPending('loop_logic', objs);
}

// Reset panel state (called when a panel closes or save completes).
export function _lvResetPending() { _lvPendingChanges = {}; _lvSaveBtnEl = null; }
export function _lvSetSaveBtnEl(el) { _lvSaveBtnEl = el; }

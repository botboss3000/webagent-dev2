'use strict';

/**
 * Agents — Agent Loop (Test) tab.
 * Interactive loop diagram with node configuration panels.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { icon } from '../../../shared/js/icons.js';
import { fetchAllToolMeta, appendToolItem, createLoopView } from '../agent-loop/js/loop-logic.js';
import { NODE_PANEL_INFO } from '../agent-loop/js/loop-node-data.js';
import { LOOP_W, LOOP_NODES, TOGGLEABLE_NODES, renderLoopDiagram } from '../agent-loop/js/loop-diagram.js';
import {
  _isMockAgent, _agents,
  _toolsForAgent, TIER_2_CATEGORIES,
  _loopNodeEnabledPersisted,
  _setNodeLoopEnabled, _lvSetPending,
  _lvSetSaveBtnEl, _lvResetPending,
  _lvPendingChanges,
} from './state.js';
import { _esc, _btn } from './utils.js';

// One active node-info panel at a time
let _lvActivePanelNodeId = null;
// Panel UI state (kept local; the data state is in state.js)
let _lvActivePanelEl = null;

// The live loop-view controller mounted in THIS tab (one at a time). It streams
// the active chat's loop, but only when the active chat's agent matches the agent
// on this card (the gate); otherwise it shows the blueprint. See loop-logic.js
// createLoopView + docs/claude/agent-loop.md.
let _agentTabView = null;

// ── Main render ───────────────────────────────────────────────────────────────

export function _renderTestTab(body, agent) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to test it in the loop.</div>';
    return;
  }
  const area = document.createElement('div'); area.className = 'agents-test-area';
  area.innerHTML = `
    <div class="agents-test-input-row">
      <input class="agents-input agents-test-input" placeholder="Type a test message and press Run to fire a one-off test turn\u2026" />
      <button class="agents-btn primary agents-test-run">Run</button>
    </div>
    <div class="agents-test-status"></div>
    <div class="agents-test-loop"></div>`;

  const input = area.querySelector('.agents-test-input');
  const runBtn = area.querySelector('.agents-test-run');

  runBtn.addEventListener('click', () => _runTest(agent, area));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _runTest(agent, area); }
  });
  body.appendChild(area);
  // Mount the shared live loop-view: streams the active chat's loop when its agent
  // matches THIS card, otherwise shows the blueprint. (Run pill above = fallback.)
  _mountLoopArea(area, agent);
}

// \u2500\u2500 Live loop-view mount \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
// Creates (or replaces) the shared streaming controller for this tab. The gate
// makes it stream only when the active chat belongs to this same agent; when it
// doesn't, onBlocked draws the static blueprint + a hint instead.
function _mountLoopArea(area, agent) {
  if (_agentTabView) { try { _agentTabView.destroy(); } catch (_) {} _agentTabView = null; }
  const loopEl = area.querySelector('.agents-test-loop');
  if (!loopEl) return;
  _disconnectLoopObservers(loopEl);

  _agentTabView = createLoopView({
    markerPrefix: 'agl',
    showOptimizer: true,
    gate:        () => !!app.currentAgentId && app.currentAgentId === agent.id,
    isAlive:     () => document.body.contains(loopEl),
    getGraphArea: () => _ensureLiveScaffold(loopEl).graph,
    getPages:     () => loopEl.querySelector('.agents-loop-pages'),
    getSidebar:   () => null,
    onBlocked:    () => _drawBlueprint(loopEl, agent),
    onNodeClick:  (nd, el, root) => _lvShowPanel(nd, el, root, agent),
    decorateNode: (nd, el) => {
      if (agent?.source === 'custom' && nd.id !== 'user_input' && nd.id !== 'final_response' && nd.id !== 'validate_tools') el.classList.add('lv-node-editable');
      if (TOGGLEABLE_NODES.has(nd.id)) el.classList.add('lv-node-toggleable');
    },
  });
  _agentTabView.start();
}

// Build the live chrome (toolbar + pages bar + graph area) inside the loop panel
// once; reuse it thereafter. Returns the graph + pages elements the view renders into.
function _ensureLiveScaffold(loopEl) {
  let container = loopEl.querySelector(':scope > .loop-visual-container');
  if (!container) {
    _disconnectLoopObservers(loopEl);
    loopEl.classList.add('is-live');
    loopEl.innerHTML = `
      <div class="loop-visual-container">
        <div class="loop-visual-toolbar">
          <span class="agents-loop-live-label">${icon('repeat', { size: '12px' })} Agent Loop <span class="agents-loop-live-dot"></span> live from active chat</span>
          <div class="loop-visual-pages agents-loop-pages"></div>
          <div class="loop-visual-legend">
            <span class="lv-legend-item"><span class="lv-legend-dot" style="background:#2a2a4a;"></span> idle</span>
            <span class="lv-legend-item"><span class="lv-legend-dot" style="background:#7dcfff;"></span> active</span>
            <span class="lv-legend-item"><span class="lv-legend-dot" style="background:#9ece6a;"></span> done</span>
            <span class="lv-legend-item"><span class="lv-legend-dot" style="background:#f7768e;"></span> error</span>
          </div>
        </div>
        <div class="loop-visual-graph-area"><div class="loop-visual-hint">Loading\u2026</div></div>
      </div>`;
    container = loopEl.querySelector(':scope > .loop-visual-container');
  }
  return {
    graph: container.querySelector('.loop-visual-graph-area'),
    pages: container.querySelector('.agents-loop-pages'),
  };
}

// Blocked / fallback state \u2014 active chat isn't this agent: show the static
// blueprint diagram (still clickable for editing) plus a hint.
function _drawBlueprint(loopEl, agent) {
  _disconnectLoopObservers(loopEl);
  loopEl.classList.remove('is-live');
  loopEl.innerHTML = '';
  const hint = document.createElement('div');
  hint.className = 'agents-loop-blueprint-hint';
  hint.innerHTML = `${icon('info', { size: '12px' })} Open or switch your chat to this agent to watch its loop run live \u2014 showing the blueprint.`;
  loopEl.appendChild(hint);
  const host = document.createElement('div');
  host.className = 'agents-loop-blueprint-host';
  loopEl.appendChild(host);
  _drawAgentLoopDiagram(host, new Map(), agent);
}

// Disconnect any ResizeObservers left on the loop panel by a prior mode (live
// graph area or blueprint host) before we swap its contents.
function _disconnectLoopObservers(loopEl) {
  loopEl._lvRo?.disconnect();
  loopEl.querySelector('.loop-visual-graph-area')?._lvRo?.disconnect();
  loopEl.querySelector(':scope > .agents-loop-blueprint-host')?._lvRo?.disconnect();
}

async function _runTest(agent, areaEl) {
  const input = areaEl.querySelector('.agents-test-input');
  const status = areaEl.querySelector('.agents-test-status');
  const loopEl = areaEl.querySelector('.agents-test-loop');
  if (!input || !status || !loopEl) return;
  const msg = input.value.trim();
  if (!msg) return;

  // A test run takes over the loop panel \u2014 tear down the live view and draw onto a
  // plain surface. The Back button re-mounts (returns to live or blueprint).
  _toBlueprintSurface(loopEl);
  status.innerHTML = `${icon('loader-2', { size: '13px' })} Running\u2026`;
  _drawAgentLoopDiagram(loopEl, new Map([
    ['user_input', 'active'], ['load_context', 'active'], ['memory_search', 'active'],
    ['build_prompt', 'active'], ['llm_call', 'active'],
  ]), agent);

  const back = () => { status.textContent = ''; _mountLoopArea(areaEl, agent); };
  try {
    const res = await fetch('/api/v1/agents/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: app.currentUserId, agent_id: agent.id, message: msg }),
    });
    const data = await res.json();
    if (!res.ok) { _drawAgentLoopDiagram(loopEl, new Map([['llm_call', 'error']]), agent); status.innerHTML = `Error ${res.status}: ${_esc(data.detail || 'unknown')} &nbsp;`; const resetBtn = _backBtn(); resetBtn.addEventListener('click', back); status.appendChild(resetBtn); return; }
    const rows = data.interactions || [];
    _drawAgentLoopDiagram(loopEl, _interactionsToNodeStates(rows), agent);
    status.innerHTML = `\u2713 Complete \u2014 ${rows.length} step(s) &nbsp;`;
    const resetBtn = _backBtn(); resetBtn.addEventListener('click', back); status.appendChild(resetBtn);
  } catch (e) { _drawAgentLoopDiagram(loopEl, new Map([['llm_call', 'error']]), agent); status.innerHTML = `Error: ${_esc(e.message)} &nbsp;`; const resetBtn = _backBtn(); resetBtn.addEventListener('click', back); status.appendChild(resetBtn); }
}

// Reset the loop panel to a plain drawing surface (used by a test run): drop the
// live view + chrome so _drawAgentLoopDiagram can draw directly into it.
function _toBlueprintSurface(loopEl) {
  if (_agentTabView) { try { _agentTabView.destroy(); } catch (_) {} _agentTabView = null; }
  _disconnectLoopObservers(loopEl);
  loopEl.classList.remove('is-live');
  loopEl.innerHTML = '';
}

function _backBtn() {
  const b = document.createElement('button'); b.className = 'agents-blueprint-back-inline';
  b.textContent = '\u2190 Back to loop'; return b;
}

// ── Loop diagram ──────────────────────────────────────────────────────────────

function _triggerExclusions(agent) {
  const tt = agent?.trigger_type || 'user_input';
  if (tt === 'user_input') return { excludeNodes: ['slash_cmd'], extraEdges: [{ from: 'user_input', to: 'session_setup' }], nodeLabelMap: {} };
  if (tt === 'slash_command') return { excludeNodes: [], extraEdges: [], nodeLabelMap: { slash_cmd: 'Slash Trigger' } };
  return { excludeNodes: [], extraEdges: [], nodeLabelMap: {} };
}

function _drawAgentLoopDiagram(loopEl, nodeStates, agent) {
  loopEl._lvRo?.disconnect();
  const savedScroll = loopEl.scrollTop;
  loopEl.innerHTML = '';
  _lvHidePanel();
  loopEl._lvNodeStates = nodeStates; loopEl._lvAgent = agent;

  const scaleWrap = document.createElement('div');
  scaleWrap.style.cssText = 'width:100%;overflow:hidden;';
  loopEl.appendChild(scaleWrap);

  const availableWidth = Math.max(300, scaleWrap.clientWidth || scaleWrap.offsetWidth || LOOP_W);
  const { excludeNodes, extraEdges, nodeLabelMap } = _triggerExclusions(agent);
  const _ll = Array.isArray(agent?.loop_logic) ? agent.loop_logic : [];
  const _llFlat = _ll.length > 0 && typeof _ll[0] === 'string';
  const _nodeFilter = (agent && _llFlat && !_ll[0].startsWith('opt_')) ? _ll : null;
  const _nodeConfig = (!_llFlat && _ll.length > 0) ? new Map(_ll.map(item => [item.node, { enabled: item.enabled !== false }])) : null;

  const { rootEl } = renderLoopDiagram(scaleWrap, nodeStates, {
    availableWidth, markerPrefix: 'ag', nodeFilter: _nodeFilter, nodeConfig: _nodeConfig,
    excludeNodes, extraEdges, nodeLabelMap,
    getNodeDetail: nd => _lvNodeHint(nd, agent),
    onNodeClick: (nd, el, root) => {
      if (_lvActivePanelNodeId === nd.id) { _lvHidePanel(true); return; }
      _lvShowPanel(nd, el, root, agent);
    },
    decorateNode: (nd, el) => {
      if (agent?.source === 'custom' && nd.id !== 'user_input' && nd.id !== 'final_response' && nd.id !== 'validate_tools') el.classList.add('lv-node-editable');
      if (TOGGLEABLE_NODES.has(nd.id)) el.classList.add('lv-node-toggleable');
    },
  });

  rootEl.addEventListener('click', _lvOutsideClickHandler);
  if (savedScroll > 0) requestAnimationFrame(() => { loopEl.scrollTop = savedScroll; });

  loopEl._lvRo = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect?.width ?? 0;
    if (w && Math.abs(w - (loopEl._lvLastRoWidth || 0)) < 2) return;
    loopEl._lvLastRoWidth = w;
    clearTimeout(loopEl._lvResizeTimer);
    loopEl._lvResizeTimer = setTimeout(() => {
      if (_lvActivePanelEl) return;
      _drawAgentLoopDiagram(loopEl, loopEl._lvNodeStates, loopEl._lvAgent);
    }, 120);
  });
  if (loopEl.parentElement) loopEl._lvRo.observe(loopEl.parentElement);
  else loopEl._lvRo.observe(loopEl);
}

// ── Node hint ─────────────────────────────────────────────────────────────────

function _lvNodeHint(nd, agent) {
  if (!agent) return '';
  const isCustom = agent.source === 'custom';
  const hints = {
    user_input: 'User message enters the pipeline here',
    slash_cmd: 'Intercept check \u2014 /optimize routes directly to the optimizer',
    session_setup: isCustom ? `Agent: ${agent.name || agent.id} \u2014 session init` : 'Ensures session exists, resolves agent',
    save_user_msg: 'Persists the user message as role="user" before the loop starts',
    load_context: isCustom ? 'Click to edit prompt sections' : 'Loads context documents',
    memory_search: 'Semantic search over past sessions',
    resolve_attach: 'Resolves uploaded file IDs into metadata',
    attachment_describe: isCustom ? 'Click to toggle image descriptions' : 'Describe images for non-vision models',
    build_prompt: isCustom ? 'Click to edit prompt sections' : 'Assembles system prompt',
    build_history: 'Loads session history \u2192 OpenAI format',
    load_provider: isCustom ? `${agent.model || 'claude-3-5-sonnet'} \u2014 click to configure` : `LLM config: ${agent.model || 'claude-3-5-sonnet'}`,
    load_tools: isCustom ? `${_toolsForAgent(agent).length} tools \u2014 click to manage` : `${_toolsForAgent(agent).length} tools loaded`,
    assemble_msgs: 'Builds messages array: [system, ...history, {role:"user"}]',
    interrupt_chk: 'Checks for cancellation signal',
    turn_counter: isCustom ? `Max turns: ${agent.max_turn_count || '\u221e (unlimited)'}` : `Max turns: ${agent.max_turn_count || '\u221e'}`,
    build_tool_defs: 'Converts tool metadata into OpenAI tool_calls schema',
    llm_call: isCustom ? `${agent.model || 'claude-3-5-sonnet'} \u2014 click to configure` : `Model: ${agent.model || 'claude-3-5-sonnet'}`,
    db_persist_asst: 'Saves assistant message to DB before validation',
    validate_tools: 'Validates each tool call',
    destructive_chk: 'Checks DESTRUCTIVE_TOOLS set',
    guardrails: isCustom ? 'Click to configure tool guardrails' : 'Destructive tools require confirmation',
    post_val_chk: 'Interrupt check after validation loop',
    execute_tools: isCustom ? `${_toolsForAgent(agent).length} tools \u2014 click to manage` : `${_toolsForAgent(agent).length} tools available`,
    db_persist_tool: 'Saves tool result as role="tool"',
    delegation_chk: 'Checks for __delegate__ sentinel',
    skill_track: 'Records tool execution event',
    check_continue: isCustom ? `Max turns: ${agent.max_turn_count || '\u221e'}` : `Loops back if tool results exist`,
    final_response: 'Final reply streamed to the user over WebSocket',
    db_persist_final: 'Saves final assistant response to interactions table',
    memory_save: isCustom ? 'Memory save \u2014 click to toggle' : 'Key facts saved to long-term memory',

  };
  return hints[nd.id] || '';
}

// ── Panel system ──────────────────────────────────────────────────────────────

function _lvHidePanel(force = false) {
  if (!force && Object.keys(_lvPendingChanges).length > 0) {
    if (!confirm('You have unsaved changes \u2014 discard them?')) return;
  }
  if (_lvActivePanelEl) { _lvActivePanelEl.remove(); _lvActivePanelEl = null; }
  _lvActivePanelNodeId = null; _lvResetPending();
  document.removeEventListener('click', _lvOutsideClickHandler);
}

function _lvOutsideClickHandler() {
  if (Object.keys(_lvPendingChanges).length > 0) {
    if (!confirm('You have unsaved changes \u2014 discard them?')) return;
  }
  _lvHidePanel(true);
}

function _lvShowPanel(nd, nodeEl, container, agent) {
  if (agent && agent.source === 'custom') _lvShowEditPanel(nd, nodeEl, container, agent);
  else _lvShowReadOnlyPanel(nd, nodeEl, container, agent);
}

function _lvShowReadOnlyPanel(nd, nodeEl, container, agent) {
  _lvHidePanel();
  const panel = document.createElement('div'); panel.className = 'lv-tool-panel';
  const header = document.createElement('div'); header.className = 'lv-tool-panel-header';
  const title = document.createElement('span'); title.className = 'lv-tool-panel-title'; title.textContent = nd.label;
  const close = document.createElement('button'); close.className = 'lv-tool-panel-close';
  close.innerHTML = icon('x', { size: '14px' });
  close.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(); });
  header.appendChild(title); header.appendChild(close);
  panel.appendChild(header);
  _lvRenderNodeInfo(panel, nd);
  // Render extras per node
  if (nd.id === 'session_setup' || nd.id === 'load_context' || nd.id === 'build_prompt') {
    const lbl = document.createElement('div'); lbl.className = 'lv-tool-section-label';
    lbl.textContent = 'Prompt sections';
    panel.appendChild(lbl);
    const list = document.createElement('div'); list.className = 'lv-tool-panel-list';
    ['agent_prompt','user_prompt','skills_prompt','tasks_prompt','misc_prompt'].forEach(key => {
      const filled = agent[key] && String(agent[key]).trim();
      appendToolItem(list, { name: key, type: filled ? 'tool' : 'command', desc: filled ? String(agent[key]).trim().substring(0, 80) + '\u2026' : '(empty)' });
    });
    panel.appendChild(list);
  } else if (nd.id === 'memory_search') {
    const lbl = document.createElement('div'); lbl.className = 'lv-tool-section-label'; lbl.textContent = 'Status';
    panel.appendChild(lbl);
    const list = document.createElement('div'); list.className = 'lv-tool-panel-list';
    const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
    const on = !disabled.has('memory');
    appendToolItem(list, { name: 'Memory search', type: on ? 'tool' : 'command', desc: on ? 'Enabled' : 'Disabled' });
    panel.appendChild(list);
  } else if (nd.id === 'load_tools' || nd.id === 'execute_tools') {
    const count = _toolsForAgent(agent).length;
    const lbl = document.createElement('div'); lbl.className = 'lv-tool-section-label lv-tool-section-live';
    lbl.innerHTML = `Tools (${count}) <span class="lv-live-dot"></span>`;
    panel.appendChild(lbl);
    const list = document.createElement('div'); list.className = 'lv-tool-panel-list';
    const loadingEl = document.createElement('div'); loadingEl.className = 'lv-tool-panel-empty lv-tool-loading';
    loadingEl.textContent = 'Loading\u2026'; list.appendChild(loadingEl); panel.appendChild(list);
    const agentToolNames = new Set(_toolsForAgent(agent));
    fetchAllToolMeta().then(allTools => {
      const nodeTools = allTools.filter(t => agentToolNames.has(t.name));
      list.innerHTML = '';
      if (!nodeTools.length) { const none = document.createElement('div'); none.className = 'lv-tool-panel-empty'; none.textContent = 'No tools for this agent.'; list.appendChild(none); return; }
      nodeTools.sort((a, b) => { const aS = a.source === 'skill' ? 0 : 1; const bS = b.source === 'skill' ? 0 : 1; return aS - bS || a.name.localeCompare(b.name); });
      nodeTools.forEach(t => appendToolItem(list, { name: t.name, type: t.destructive ? 'guarded' : t.source === 'skill' ? 'skill' : 'tool', desc: t.description || '' }));
    });
  }
  panel.addEventListener('click', e => e.stopPropagation());
  const _outerLvA = container.closest('.agents-test-area') || container;
  _outerLvA.insertBefore(panel, _outerLvA.querySelector('.agents-test-loop'));
  _lvActivePanelNodeId = nd.id; _lvActivePanelEl = panel;
}

function _lvShowEditPanel(nd, nodeEl, container, agent) {
  _lvHidePanel(true);
  const panel = document.createElement('div'); panel.className = 'lv-edit-panel';
  panel.addEventListener('click', e => e.stopPropagation());
  const header = document.createElement('div'); header.className = 'lv-tool-panel-header';
  const hLeft = document.createElement('div'); hLeft.style.cssText = 'display:flex;align-items:center;gap:7px;';
  const title = document.createElement('span'); title.className = 'lv-tool-panel-title'; title.textContent = nd.label;
  const badge = document.createElement('span'); badge.className = 'lv-edit-badge'; badge.textContent = 'editable';
  hLeft.appendChild(title); hLeft.appendChild(badge);
  const close = document.createElement('button'); close.className = 'lv-tool-panel-close';
  close.innerHTML = icon('x', { size: '14px' });
  close.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(); });
  header.appendChild(hLeft); header.appendChild(close);
  panel.appendChild(header);

  const body = document.createElement('div'); body.className = 'lv-edit-body';
  panel.appendChild(body);

  // Route to the correct node editor
  switch (nd.id) {
    case 'load_context':   body.innerHTML = '<div class="lv-edit-desc">Loads context documents. Click a prompt tab to edit.</div>'; break;
    case 'build_prompt':   body.innerHTML = '<div class="lv-edit-desc">Assembles system prompt. Edit in the Prompts tab.</div>'; break;
    case 'build_history':  body.innerHTML = '<div class="lv-edit-desc">Loads history from the database.</div>'; break;
    case 'load_tools':     body.innerHTML = '<div class="lv-edit-desc">Manages tool loading. Edit in the Tools tab.</div>'; break;
    case 'assemble_msgs':  body.innerHTML = '<div class="lv-edit-desc">Combines system prompt, history, and current message.</div>'; break;
    case 'session_setup':  body.innerHTML = '<div class="lv-edit-desc">Session init, agent resolve, participants.</div>'; break;
    case 'memory_search':  _lvRenderMemorySearchEditor(body, agent); break;
    case 'llm_call':       _lvRenderLlmEditor(body, agent); break;
    case 'execute_tools':  _lvRenderToolsEditor(body, agent); break;
    case 'guardrails':     _lvRenderGuardrailsEditor(body, agent); break;
    case 'turn_counter':
    case 'permission_chk':
    case 'check_continue': _lvRenderContinueEditor(body, agent); break;
    case 'memory_save':    _lvRenderMemorySaveEditor(body, agent); break;
    case 'interrupt_chk':  _lvRenderGatedNodeEditor(body, agent, 'interrupt_chk', 'Interrupt check', 'Polls for cancellation signal.', {}); break;
    case 'delegation_chk': _lvRenderGatedNodeEditor(body, agent, 'delegation_chk', 'Agent delegation', 'Checks for __delegate__ sentinel.', {}); break;
    case 'skill_track':    _lvRenderGatedNodeEditor(body, agent, 'skill_track', 'Skill tracking', 'Records tool execution event.', {}); break;
    default: { _lvRenderNodeInfo(body, nd); const info = document.createElement('div'); info.className = 'lv-edit-desc'; info.textContent = _lvNodeHint(nd, agent) || 'No editable settings.'; body.appendChild(info); }
  }

  // Save bar
  const _INFO_NODES = new Set(['load_context', 'build_prompt', 'build_history', 'load_tools', 'assemble_msgs']);
  if (!_INFO_NODES.has(nd.id)) {
    const saveBar = document.createElement('div'); saveBar.className = 'lv-edit-save-bar';
    const saveMsg = document.createElement('span'); saveMsg.className = 'lv-edit-save-msg';
    const cancelBtn = document.createElement('button'); cancelBtn.className = 'lv-edit-cancel-btn';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', e => { e.stopPropagation(); _lvHidePanel(true); });
    const saveBtn = document.createElement('button'); saveBtn.className = 'lv-edit-save-btn';
    saveBtn.textContent = 'Save changes';
    _lvSetSaveBtnEl(saveBtn);

    saveBtn.addEventListener('click', async e => {
      e.stopPropagation();
      if (!Object.keys(_lvPendingChanges).length) { saveMsg.textContent = 'No changes'; return; }
      saveBtn.disabled = true; cancelBtn.disabled = true; saveMsg.textContent = 'Saving\u2026';
      try {
        const res = await fetch(`/api/v1/agents/${agent.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ user_id: app.currentUserId, ..._lvPendingChanges }),
        });
        const data = await res.json();
        if (res.ok) {
          const idx = _agents.findIndex(a => a.id === agent.id);
          if (idx !== -1) Object.assign(_agents[idx], data.agent);
          Object.assign(agent, data.agent);
          saveMsg.textContent = '\u2713 Saved'; saveMsg.className = 'lv-edit-save-msg ok';
          _lvResetPending();
          const loopEl = panel.closest('.agents-test-area')?.querySelector('.agents-test-loop');
          if (loopEl) _drawAgentLoopDiagram(loopEl, new Map(), agent);
        } else { saveMsg.textContent = data.detail || 'Save failed'; saveMsg.className = 'lv-edit-save-msg error'; }
      } catch (err) { saveMsg.textContent = `Error: ${err.message}`; saveMsg.className = 'lv-edit-save-msg error'; }
      saveBtn.disabled = false; cancelBtn.disabled = false;
    });

    saveBar.appendChild(saveMsg); saveBar.appendChild(cancelBtn); saveBar.appendChild(saveBtn);
    panel.appendChild(saveBar);
  }

  const _outerLvB = container.closest('.agents-test-area') || container;
  _outerLvB.insertBefore(panel, _outerLvB.querySelector('.agents-test-loop'));
  _lvActivePanelNodeId = nd.id; _lvActivePanelEl = panel;
}

// ── Editor helpers ────────────────────────────────────────────────────────────

function _lvRenderNodeInfo(panel, nd) {
  const info = NODE_PANEL_INFO[nd.id];
  if (!info) return;
  const descEl = document.createElement('div'); descEl.className = 'lv-edit-desc'; descEl.textContent = info.desc;
  panel.appendChild(descEl);
  if (info.details && info.details.length) {
    const lbl = document.createElement('div'); lbl.className = 'lv-tool-section-label'; lbl.textContent = 'Details';
    panel.appendChild(lbl);
    const list = document.createElement('div'); list.className = 'lv-tool-panel-list';
    info.details.forEach(d => {
      const item = document.createElement('div'); item.className = 'lv-tool-item';
      const nameEl = document.createElement('div'); nameEl.className = 'lv-tool-name'; nameEl.textContent = d.key;
      const valEl = document.createElement('div'); valEl.className = 'lv-tool-desc'; valEl.textContent = d.val;
      item.appendChild(nameEl); item.appendChild(valEl); list.appendChild(item);
    });
    panel.appendChild(list);
  }
}

function _lvRenderMemorySearchEditor(body, agent) {
  body.innerHTML = '<div class="lv-edit-desc">Controls memory recall behavior.</div>';
  _lvToggleRow(body, 'Memory search node', _loopNodeEnabledPersisted(agent, 'memory_search'), on => { _setNodeLoopEnabled(agent, 'memory_search', on); });
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const memEnabled = !disabled.has('memory');
  _lvToggleRow(body, 'Memory tool access', memEnabled, enabled => {
    const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools) ? _lvPendingChanges.allowed_tools : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
    if (enabled) cur.delete('memory'); else cur.add('memory');
    _lvSetPending('allowed_tools', [...cur]);
  });
}

function _lvRenderLlmEditor(body, agent) {
  body.innerHTML = '<div class="lv-edit-desc">Configure the language model.</div>';
  const MODELS = ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022', 'claude-3-opus-20240229'];
  _lvSelectRow(body, 'Model', agent.model || 'claude-3-5-sonnet-20241022', MODELS, val => _lvSetPending('model', val));
  _lvSliderRow(body, 'Temperature', agent.temperature ?? 1.0, 0, 1, 0.05, val => _lvSetPending('temperature', Math.round(val * 100) / 100));
  _lvSliderRow(body, 'Max tokens', agent.max_tokens ?? 8096, 512, 16384, 512, val => _lvSetPending('max_tokens', parseInt(val, 10)));
}

function _lvRenderToolsEditor(body, agent) {
  body.innerHTML = '<div class="lv-edit-desc">Enable or disable Tier-2 tools.</div>';
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  TIER_2_CATEGORIES.forEach(cat => {
    const catLabel = document.createElement('div'); catLabel.className = 'lv-edit-cat-label'; catLabel.textContent = cat.label;
    body.appendChild(catLabel);
    cat.tools.forEach(tool => {
      const enabled = !disabled.has(tool.name);
      _lvToolToggleRow(body, tool, enabled, isOn => {
        const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools) ? _lvPendingChanges.allowed_tools : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
        if (isOn) cur.delete(tool.name); else cur.add(tool.name);
        _lvSetPending('allowed_tools', [...cur]);
      });
    });
  });
}

function _lvRenderGuardrailsEditor(body, agent) {
  body.innerHTML = '<div class="lv-edit-desc">Configure destructive-tool guardrail.</div>';
  const guardEnabled = _loopNodeEnabledPersisted(agent, 'guardrails');
  _lvToggleRow(body, 'Require confirmation for destructive tools', guardEnabled, on => _setNodeLoopEnabled(agent, 'guardrails', on));
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  TIER_2_CATEGORIES.forEach(cat => {
    const allNames = cat.tools.map(t => t.name);
    const allBlocked = allNames.every(n => disabled.has(n));
    _lvToggleRow(body, `Allow: ${cat.label}`, !allBlocked, allowed => {
      const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools) ? _lvPendingChanges.allowed_tools : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
      if (allowed) allNames.forEach(n => cur.delete(n)); else allNames.forEach(n => cur.add(n));
      _lvSetPending('allowed_tools', [...cur]);
    });
  });
}

function _lvRenderContinueEditor(body, agent) {
  body.innerHTML = '<div class="lv-edit-desc">Set max agentic turns.</div>';
  _lvSliderRow(body, 'Max turns', agent.max_turn_count ?? 0, 0, 30, 1, val => _lvSetPending('max_turn_count', parseInt(val, 10)));
  const permEnabled = _loopNodeEnabledPersisted(agent, 'permission_chk');
  _lvToggleRow(body, 'Ask permission before stopping', permEnabled, on => _setNodeLoopEnabled(agent, 'permission_chk', on));
}

function _lvRenderMemorySaveEditor(body, agent) {
  body.innerHTML = '<div class="lv-edit-desc">Control memory save behavior.</div>';
  const disabled = new Set(Array.isArray(agent.allowed_tools) ? agent.allowed_tools : []);
  const saveEnabled = !disabled.has('memory_save');
  _lvToggleRow(body, 'Save facts to long-term memory', saveEnabled, enabled => {
    const cur = new Set(Array.isArray(_lvPendingChanges.allowed_tools) ? _lvPendingChanges.allowed_tools : Array.isArray(agent.allowed_tools) ? [...agent.allowed_tools] : []);
    if (enabled) cur.delete('memory_save'); else cur.add('memory_save');
    _lvSetPending('allowed_tools', [...cur]);
  });
}

function _lvRenderGatedNodeEditor(body, agent, nodeId, label, description, details) {
  body.innerHTML = `<div class="lv-edit-desc">${_esc(description)}</div>`;
  const enabled = _loopNodeEnabledPersisted(agent, nodeId);
  _lvToggleRow(body, label, enabled, on => _setNodeLoopEnabled(agent, nodeId, on));
}

// ── UI widgets ────────────────────────────────────────────────────────────────

function _lvToggleRow(container, label, initialValue, onChange) {
  const row = document.createElement('div'); row.className = 'lv-edit-toggle-row';
  const lbl = document.createElement('span'); lbl.className = 'lv-edit-toggle-label'; lbl.textContent = label;
  const tog = document.createElement('button'); tog.className = 'lv-edit-toggle';
  tog.dataset.on = initialValue ? '1' : '0'; tog.textContent = initialValue ? 'ON' : 'OFF';
  tog.addEventListener('click', e => {
    e.stopPropagation();
    const nowOn = tog.dataset.on !== '1'; tog.dataset.on = nowOn ? '1' : '0';
    tog.textContent = nowOn ? 'ON' : 'OFF'; onChange(nowOn);
  });
  row.appendChild(lbl); row.appendChild(tog); container.appendChild(row);
}

function _lvToolToggleRow(container, tool, enabled, onChange) {
  const row = document.createElement('div'); row.className = 'lv-edit-tool-row';
  const nameEl = document.createElement('span'); nameEl.className = 'lv-edit-tool-name'; nameEl.textContent = tool.name;
  const left = document.createElement('div'); left.className = 'lv-edit-tool-left';
  left.appendChild(nameEl); if (tool.destructive) { const bdg = document.createElement('span'); bdg.className = 'lv-edit-tool-badge'; bdg.textContent = '\u{1F6E1}'; left.appendChild(bdg); }
  const descEl = document.createElement('div'); descEl.className = 'lv-edit-tool-desc'; descEl.textContent = tool.desc;
  const tog = document.createElement('button'); tog.className = 'lv-edit-toggle small';
  tog.dataset.on = enabled ? '1' : '0'; tog.textContent = enabled ? 'ON' : 'OFF';
  tog.addEventListener('click', e => {
    e.stopPropagation();
    const nowOn = tog.dataset.on !== '1'; tog.dataset.on = nowOn ? '1' : '0';
    tog.textContent = nowOn ? 'ON' : 'OFF'; onChange(nowOn);
  });
  const nameRow = document.createElement('div'); nameRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;';
  const leftBlock = document.createElement('div'); leftBlock.appendChild(left); leftBlock.appendChild(descEl);
  nameRow.appendChild(leftBlock); nameRow.appendChild(tog); row.appendChild(nameRow); container.appendChild(row);
}

function _lvSliderRow(container, label, initialValue, min, max, step, onChange) {
  const row = document.createElement('div'); row.className = 'lv-edit-slider-row';
  const labelRow = document.createElement('div'); labelRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;';
  const lbl = document.createElement('span'); lbl.className = 'lv-edit-toggle-label'; lbl.textContent = label;
  const valEl = document.createElement('span'); valEl.className = 'lv-edit-slider-val'; valEl.textContent = initialValue;
  labelRow.appendChild(lbl); labelRow.appendChild(valEl);
  const slider = document.createElement('input'); slider.type = 'range'; slider.className = 'lv-edit-slider';
  slider.min = min; slider.max = max; slider.step = step; slider.value = initialValue;
  slider.addEventListener('input', () => { const v = parseFloat(slider.value); valEl.textContent = step < 1 ? v.toFixed(2) : slider.value; onChange(v); });
  row.appendChild(labelRow); row.appendChild(slider); container.appendChild(row);
}

function _lvSelectRow(container, label, initialValue, options, onChange) {
  const row = document.createElement('div'); row.className = 'lv-edit-select-row';
  const lbl = document.createElement('div'); lbl.className = 'lv-edit-toggle-label'; lbl.textContent = label; lbl.style.marginBottom = '4px';
  const sel = document.createElement('select'); sel.className = 'lv-edit-select';
  options.forEach(opt => { const o = document.createElement('option'); o.value = opt; o.textContent = opt; if (opt === initialValue) o.selected = true; sel.appendChild(o); });
  sel.addEventListener('change', () => onChange(sel.value));
  row.appendChild(lbl); row.appendChild(sel); container.appendChild(row);
}

// _lvAppendItem replaced by shared appendToolItem from loop-logic.js

function _interactionsToNodeStates(rows) {
  const s = new Map();
  for (const row of rows) {
    const role = row.role || ''; const toolName = row.tool_name || '';
    if (role === 'user') { s.set('user_input', 'done'); s.set('load_context', 'done'); s.set('memory_search', 'done'); s.set('build_prompt', 'done'); }
    else if (role === 'assistant') { s.set('llm_call', 'done'); s.set('validate_tools', 'done'); s.set('check_continue', 'done'); s.set('final_response', 'done'); }
    else if (role === 'tool') {
      if (toolName === 'memory_search') s.set('memory_search', 'done');
      else if (toolName === 'memory_save') s.set('memory_save', 'done');
      else { let meta = {}; try { meta = JSON.parse(row.metadata || '{}'); } catch (_) {} const success = meta.success !== false; s.set('validate_tools', 'done'); s.set('guardrails', 'done'); s.set('execute_tools', success ? 'done' : 'error'); s.set('check_continue', 'done'); }
    }
  }
  return s;
}

// Re-export for use by state.js via dynamic import

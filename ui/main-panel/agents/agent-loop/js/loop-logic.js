'use strict';

// Runtime Loop visualizer — the agent decision-graph view: renders the loop
// diagram (loop-diagram.js) with live node states from a session's interactions,
// plus the optimizer overlay. Sibling of the Interactions sidebar (loop.js);
// shared node/topology data lives in loop-diagram.js / loop-node-data.js.
//
// ── Shared controller ────────────────────────────────────────────────────────
// The streaming engine is a reusable controller, createLoopView(opts), so the
// SAME code drives two mounts (see docs/claude/agent-loop.md):
//   • PRIMARY  — the agent card's "Agent Loop" tab (tab-agent-loop.js), gated so
//                it only streams when the active chat's agent matches the card.
//   • SECONDARY— the admin Runtime Loop page (runtime-loop-view.js), ungated.
// The old module-level functions (initLoopVisual / startLoopVisual / stopLoopVisual
// / loopVisualSessionChanged / renderRuntimeLoopSidebar) are kept as thin wrappers
// that operate the admin view and fan session-changes out to EVERY live view.
// Browser events let this module stay lazy; every view decides for itself
// (gate + isAlive) whether
// the event is "for me".

import { app } from '../../../../shared/js/state.js';
import { apiPath } from '../../../../shared/js/config.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { LOOP_W, LOOP_NODES, computeEdgePath, renderLoopDiagram, applyDiagramStates } from './loop-diagram.js';
import { NODE_STATIC_ITEMS, OPTIMIZER_NODES, NODE_PANEL_INFO } from './loop-node-data.js';
import { interactionToEvents, fetchInteractionRows } from './loop-events.js';
import { readAgentCache, writeAgentCache } from '../../js/agent-cache.js';

// LOOP_W imported from loop-diagram.js (used for optimizer label)

// ── Optimizer edges ──
const OPTIMIZER_EDGES = [
  { from: 'opt_collect',  to: 'opt_analyze'  },
  { from: 'opt_analyze',  to: 'opt_propose'  },
  { from: 'opt_propose',  to: 'opt_validate' },
  { from: 'opt_validate', to: 'opt_apply',    label: 'validated' },
  { from: 'opt_apply',    to: 'opt_collect',  loopback: 500 },
];

const MAX_BUFFER = 2000;

// ── Tool metadata cache (30s TTL — avoids re-fetching on every panel open) ──
// Module-level (shared across every view — it's the same /admin/tools data).
let _toolMetaCache = null;
let _toolMetaCacheTs = 0;
const TOOL_META_CACHE_MS = 30_000;

// ── Fetch all tool metadata from /admin/tools (built-ins + user skills) ──
export async function fetchAllToolMeta() {
  const now = Date.now();
  if (_toolMetaCache && (now - _toolMetaCacheTs) < TOOL_META_CACHE_MS) {
    return _toolMetaCache;
  }
  const persisted = readAgentCache('tool-metadata');
  if (!_toolMetaCache && Array.isArray(persisted)) {
    _toolMetaCache = persisted;
    _toolMetaCacheTs = now;
    _refreshToolMeta().catch(() => {});
    return _toolMetaCache;
  }
  return _refreshToolMeta();
}

async function _refreshToolMeta() {
  try {
    const res = await fetch(apiPath('/admin/tools'), { headers: authHeaders() });
    if (!res.ok) return [];
    const data = await res.json();
    _toolMetaCache = Array.isArray(data) ? data : [];
    _toolMetaCacheTs = Date.now();
    writeAgentCache('tool-metadata', _toolMetaCache, 5 * 60 * 1000);
    return _toolMetaCache;
  } catch {
    return [];
  }
}

// ── Safely parse a JSON field that may already be an array or a JSON string ──
function _parseJsonField(val, fallback) {
  if (Array.isArray(val)) return val;
  if (typeof val === 'string') {
    try { return JSON.parse(val); } catch { return fallback; }
  }
  return fallback;
}

// ── Map a pipeline/db/tool event → the loop node it lights up (pure) ──
function eventToNodeId(event) {
  const type = event.type;
  const step = event.step;

  if (type === 'pipeline') {
    switch (step) {
      // INPUT
      case 'user_message':          return 'user_input';

      // PRE-LOOP
      case 'agent_assigned':        return 'agent_resolve';

      // CONTEXT
      case 'load_context':          return 'load_context';
      case 'memory_search_skip':    return 'skip_gate';
      case 'memory_search_start':
      case 'memory_search_end':     return 'memory_search';
      case 'build_prompt':          return 'build_prompt';
      case 'attachment':            return 'resolve_attach';
      case 'attachment_describe_start':
      case 'attachment_describe_end': return 'attachment_describe';

      // LOOP INIT
      case 'load_tools':            return 'load_tools';
      case 'integration_status':    return 'integration_status';

      // INFERENCE (per-turn)
      case 'turn_start':            return 'interrupt_chk';
      case 'tool_defs_built':       return 'build_tool_defs';
      case 'llm_call_start':
      case 'llm_call_end':          return 'llm_call';

      // ROUTING
      case 'validate_start':
      case 'validate_result':       return 'validate_tools';
      case 'guardrail_check':       return 'destructive_chk';
      case 'guardrail_override':
      case 'guardrail_blocked':     return 'guardrails';
      case 'execute_batch_start':   return 'post_val_chk';

      // EXECUTION
      case 'execute_start':
      case 'execute_end':           return 'execute_tools';
      case 'agent_delegation':      return 'delegation_chk';
      case 'skill_track':           return 'skill_track';

      // CONTINUE?
      case 'check_continue':
      case 'max_turns_reached':     return 'check_continue';

      // OUTPUT
      case 'memory_save_start':
      case 'memory_save_end':       return 'memory_save';

      // CLOSER (post-run close-out LLM steps)
      case 'closer_start':          return 'llm_call';
      case 'closer_audit_start':
      case 'closer_audit_end':      return 'memory_save';
      case 'closer_llm_start':
      case 'closer_llm_end':        return 'llm_call';

      default: return null;
    }
  }

  if (type === 'attachment') return 'resolve_attach';

  if (type === 'db') {
    const op   = event.op   || '';
    const role = event.role || '';
    if (op === 'insert_interaction' && role === 'assistant') return 'db_persist_asst';
    if (op === 'insert_interaction' && role === 'tool')      return 'db_persist_tool';
    if (op === 'skill_track')                                return 'skill_track';
    if (op === 'memory_upsert')                              return 'memory_save';
    return null;
  }

  if (type === 'tool_call')  return 'execute_tools';
  if (type === 'tool_result') {
    // memory_search tool_result maps back to memory_search node
    if (event.tool === 'memory_search') return 'memory_search';
    return 'execute_tools';
  }
  if (type === 'response')   return 'final_response';
  if (type === 'error')      return 'llm_call';
  if (type === 'interrupted') return 'check_continue';
  return null;
}

// ── Derive a node state ('active' | 'done' | 'error') from an event (pure) ──
function getNodeState(event) {
  const type = event.type;
  const step = event.step;

  if (type === 'error') return 'error';
  if (type === 'tool_result' && event.error) return 'error';
  if (type === 'pipeline') {
    if (step === 'guardrail_blocked') return 'error';
    if (step === 'validate_result' && !event.passed) return 'error';
    if (step === 'llm_call_start' || step === 'execute_start' ||
        step === 'memory_search_start' || step === 'validate_start' ||
        step === 'guardrail_check' || step === 'memory_save_start') {
      return 'active';
    }
    return 'done';
  }
  if (type === 'tool_call') return 'active';
  if (type === 'tool_result') return 'done';
  if (type === 'response') return 'done';
  return 'done';
}

// ── Append a single tool row to a list element (module-level, exported) ──
export function appendToolItem(listEl, tool) {
  // badge labels — the 'command' key is used by tab-agent-loop.js for empty slots
  const BADGE_LABELS = {
    command: 'empty',
    tool:    'tool',
    guarded: '🛡 guarded',
    admin:   'admin',
    skill:   '✦ skill',
  };

  const item = document.createElement('div');
  item.className = `lv-tool-item lv-tool-${tool.type}`;

  const nameRow = document.createElement('div');
  nameRow.className = 'lv-tool-name-row';

  const badge = document.createElement('span');
  badge.className = `lv-tool-badge lv-badge-${tool.type}`;
  badge.textContent = BADGE_LABELS[tool.type] || tool.type;
  nameRow.appendChild(badge);

  const name = document.createElement('span');
  name.className = 'lv-tool-name';
  name.textContent = tool.name;
  nameRow.appendChild(name);

  item.appendChild(nameRow);

  const desc = document.createElement('div');
  desc.className = 'lv-tool-desc';
  desc.textContent = tool.desc;
  item.appendChild(desc);

  listEl.appendChild(item);
}

// ── Registry of every live view (admin + any agent-tab mounts) ──
function _ensureLoopVisualStyles() {
  if (document.querySelector("link[data-webagent-loop-styles=\"visual\"]")) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL("../loop-visual.css", import.meta.url).href;
  link.dataset.webagentLoopStyles = "visual";
  document.head.appendChild(link);
}

const _views = new Set();

window.addEventListener("webagent-loop-event", (event) => {
  for (const v of [..._views]) {
    try { v.handleEvent(event.detail); } catch (_) { /* ignore */ }
  }
});
window.addEventListener("webagent-loop-session-changed", () => {
  for (const v of [..._views]) {
    try { v.sessionChanged(); } catch (_) { /* ignore */ }
  }
});

// ═════════════════════════════════════════════════════════════════════════════
//  createLoopView — one reusable streaming visualizer instance
// ═════════════════════════════════════════════════════════════════════════════
//
// opts:
//   getGraphArea — () => HTMLElement     where the diagram renders (required-ish)
//   getPages     — () => HTMLElement|null turn-button bar
//   getSidebar   — () => {pages,nodes,refresh}|null   run-history + node list
//   gate         — () => boolean         stream only when true (default: always)
//   onBlocked    — () => void            called when gate() is false (e.g. draw a
//                                        blueprint + hint instead of streaming)
//   isAlive      — () => boolean         false → the mount's DOM is gone; the view
//                                        auto-destroys itself (default: always)
//   onNodeClick  — (nd, el, root) => void  override the built-in info panel
//   decorateNode — (nd, el) => void      extra per-node classes
//   markerPrefix — string                unique SVG marker prefix (default 'lv')
//   showOptimizer— boolean               draw the optimizer overlay (default true)
//   emptyHint    — string
export function createLoopView(opts = {}) {
  _ensureLoopVisualStyles();
  const getGraphArea = opts.getGraphArea || (() => document.getElementById('loop-visual-graph-area'));
  const getPagesEl   = opts.getPages     || (() => document.getElementById('loop-visual-pages'));
  const getSidebar   = opts.getSidebar   || (() => ({
    pages:   document.getElementById('rl-pages'),
    nodes:   document.getElementById('rl-nodes'),
    refresh: document.getElementById('rl-refresh'),
  }));
  const gate          = opts.gate         || (() => true);
  const isAlive       = opts.isAlive      || (() => true);
  const onBlocked     = opts.onBlocked    || null;
  const customClick   = opts.onNodeClick  || null;
  const decorateNode  = opts.decorateNode || null;
  const markerPrefix  = opts.markerPrefix || 'lv';
  const showOptimizer = opts.showOptimizer !== false;
  const emptyHint     = opts.emptyHint    || 'Agent loop visualizer — waiting for agent events…';

  // ── per-view state ──
  let active = false;
  let eventBuffer = [];
  let pages = [];
  let currentPageIdx = 0;
  let nextTurnNum = 0;
  let sessionLoaded = false;
  let currentNodeStates = new Map();
  let currentTurnEvents = [];
  let _renderTimer = null;
  let _activePanelNodeId = null;
  let _activePanelEl = null;

  function _resetState() {
    pages = [];
    currentPageIdx = 0;
    nextTurnNum = 0;
    currentNodeStates = new Map();
    currentTurnEvents = [];
    sessionLoaded = false;
  }

  // ── Handle events from agentWs ──
  function handleEvent(event) {
    if (!isAlive()) { destroy(); return; }
    if (!gate()) return;

    if (eventBuffer.length >= MAX_BUFFER) eventBuffer.shift();
    eventBuffer.push(event);

    if (!active) return;
    processEvent(event);
  }

  function processEvent(event) {
    const type = event.type;
    const step = event.step;

    if (type === 'pipeline' && step === 'user_message') {
      nextTurnNum++;
      startNewPage(nextTurnNum);
    }

    if (type === 'pipeline' && step === 'turn_start' && event.turn) {
      if (!pages.find(p => p.turnNum === event.turn)) {
        startNewPage(event.turn);
      }
    }

    if (pages.length === 0 && type !== 'ping') {
      nextTurnNum = 1;
      startNewPage(1);
    }

    const nodeId = eventToNodeId(event);
    if (nodeId) {
      const state = getNodeState(event);
      currentNodeStates.set(nodeId, state);
      currentTurnEvents.push({ nodeId, state, event, ts: Date.now() });

      const page = pages[currentPageIdx];
      if (page) {
        page.nodeStates.set(nodeId, state);
        page.events.push({ nodeId, state, event, ts: Date.now() });
      }
    }

    if (currentPageIdx < pages.length) {
      if (_renderTimer) cancelAnimationFrame(_renderTimer);
      _renderTimer = requestAnimationFrame(() => {
        _renderTimer = null;
        // Streaming events only change node/edge STATE, not structure — update the
        // existing diagram in place (no teardown → no scroll jump / flicker). Fall
        // back to a full rebuild only when the diagram isn't built for this page yet.
        if (!updatePageLive(currentPageIdx)) renderPage(currentPageIdx);
        updatePageSummary(currentPageIdx);
        renderSidebar();
      });
    }
  }

  function startNewPage(turnNum) {
    currentNodeStates = new Map();
    currentTurnEvents = [];

    const page = { turnNum, nodeStates: new Map(), events: [], ts: Date.now() };
    pages.push(page);
    currentPageIdx = pages.length - 1;

    renderPageButtons();
    renderPage(currentPageIdx);
    updatePageSummary(currentPageIdx);
    selectPage(currentPageIdx);
    renderSidebar();
  }

  function selectPage(idx) {
    if (idx < 0 || idx >= pages.length) return;
    currentPageIdx = idx;

    const pagesEl = getPagesEl();
    pagesEl?.querySelectorAll('.loop-visual-page-btn').forEach((btn, i) => {
      btn.classList.toggle('active', i === idx);
    });

    hideToolPanel(); // close panel on explicit page switch
    renderPage(idx);
    updatePageSummary(idx);
    renderSidebar();
  }

  function renderPageButtons() {
    const container = getPagesEl();
    if (!container) return;

    container.innerHTML = pages.map((p, i) => {
      const active = i === currentPageIdx ? 'active' : '';
      const label = p.turnNum <= 1 ? 'Initial' : `Turn ${p.turnNum - 1}`;
      return `<button class="loop-visual-page-btn ${active}" data-idx="${i}">
        <span class="page-turn-num">#${p.turnNum}</span> ${label}
      </button>`;
    }).join('');

    container.querySelectorAll('.loop-visual-page-btn').forEach(btn => {
      btn.addEventListener('click', () => selectPage(parseInt(btn.dataset.idx)));
    });
  }

  // ── Render a single page (turn) ──
  function renderPage(idx) {
    // NOTE: do NOT call hideToolPanel here — streaming events call renderPage on every
    // frame, which would destroy the panel immediately after the user opens it.
    // Panel is attached outside the area, so area rebuilds don't affect it.
    // selectPage/startNewPage call hideToolPanel explicitly on page switch.
    const area = getGraphArea();
    if (!area) return;

    const page = pages[idx];
    if (!page) {
      area.innerHTML = '<div class="loop-visual-hint">No loop data yet — waiting for agent events…</div>';
      return;
    }

    const savedScroll = area.scrollTop;
    area._lvRo?.disconnect();
    area.innerHTML = '';

    const scaleWrap = document.createElement('div');
    scaleWrap.style.cssText = 'width:100%;flex-shrink:0;overflow:hidden;';
    area.appendChild(scaleWrap);

    // Measure scaleWrap (not area) — excludes area padding and accounts for scrollbar
    const availableWidth = Math.max(300, scaleWrap.clientWidth || scaleWrap.offsetWidth || LOOP_W);

    function getNodeDetail(nd) {
      const nodeEvents = page.events.filter(e => e.nodeId === nd.id);
      if (nodeEvents.length === 0) return 'Waiting…';
      const last = nodeEvents[nodeEvents.length - 1];
      const parts = [];
      if (last.event.duration_ms)          parts.push(`${last.event.duration_ms}ms`);
      if (last.event.input_tokens)         parts.push(`↓${last.event.input_tokens}`);
      if (last.event.output_tokens)        parts.push(`↑${last.event.output_tokens}`);
      if (last.event.model)                parts.push(last.event.model);
      if (last.event.tool)                 parts.push(last.event.tool);
      if (last.event.results_count != null) parts.push(`${last.event.results_count} results`);
      return parts.length > 0
        ? parts.join(' · ')
        : `${nodeEvents.length} event${nodeEvents.length !== 1 ? 's' : ''}`;
    }

    const { rootEl, svgEl, layout } = renderLoopDiagram(scaleWrap, page.nodeStates, {
      availableWidth,
      markerPrefix,
      getNodeDetail,
      decorateNode,
      onNodeClick: (nd, el, root) => {
        if (customClick) { customClick(nd, el, root); return; }
        if (_activePanelNodeId === nd.id) hideToolPanel();
        else showToolPanel(nd, el, root);
      },
    });

    // ── Optimizer overlay (admin runtime view) ──
    if (showOptimizer) {
      const OPT_SECTION_H = 130;
      const OPT_BASE_CX   = [100, 285, 470, 658, 845];
      const optSx         = Math.min(1, availableWidth / 1000);
      const positionedOptNodes = OPTIMIZER_NODES.map((n, i) => ({ ...n, cx: Math.round(OPT_BASE_CX[i] * optSx) }));

      const totalH = layout.genuiH + OPT_SECTION_H;
      svgEl.setAttribute('height', totalH);
      svgEl.setAttribute('viewBox', `0 0 ${layout.genuiW} ${totalH}`);
      rootEl.style.minHeight = totalH + 'px';

      const divY  = layout.genuiH + 18;
      const labY  = layout.genuiH + 34;
      const optCY = layout.genuiH + 78;

      const divLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      divLine.setAttribute('x1', 10);
      divLine.setAttribute('y1', divY);
      divLine.setAttribute('x2', layout.genuiW - 10);
      divLine.setAttribute('y2', divY);
      divLine.setAttribute('stroke', '#2a2a4a');
      divLine.setAttribute('stroke-width', '1');
      divLine.setAttribute('stroke-dasharray', '4,4');
      svgEl.appendChild(divLine);

      const optLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      optLabel.setAttribute('x', 14);
      optLabel.setAttribute('y', labY);
      optLabel.setAttribute('class', 'lv-stage-label');
      optLabel.setAttribute('fill', '#9ece6a');
      optLabel.setAttribute('fill-opacity', '0.45');
      optLabel.textContent = '⚙ OPTIMIZER LOOP  —  runs on a separate schedule to improve agent skills';
      svgEl.appendChild(optLabel);

      const finalOptNodes = positionedOptNodes.map(n => ({ ...n, cy: optCY }));

      const OPT_HH = OPTIMIZER_NODES[0].hh;
      const dynOptEdges = OPTIMIZER_EDGES.map(e => e.loopback ? { ...e, loopback: optCY + OPT_HH + 30 } : e);

      for (const edge of dynOptEdges) {
        const pi = computeEdgePath(edge, finalOptNodes);
        if (!pi) continue;
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', pi.d);
        path.setAttribute('fill', 'none');
        let cls = 'lv-arrow lv-arrow-opt';
        if (edge.loopback) cls += ' lv-arrow-alt';
        path.setAttribute('class', cls);
        path.setAttribute('marker-end', `url(#${markerPrefix}-ah-opt)`);
        svgEl.appendChild(path);

        if (edge.label) {
          const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          text.setAttribute('x', pi.labelX);
          text.setAttribute('y', pi.labelY);
          text.setAttribute('text-anchor', 'middle');
          text.setAttribute('class', 'lv-arrow-label lv-arrow-label-opt');
          text.textContent = edge.label;
          svgEl.appendChild(text);
        }
      }

      for (const nodeDef of finalOptNodes) {
        renderNodeEl(nodeDef, rootEl);
      }
    }

    // Stash what updatePageLive() needs to refresh this diagram in place on the next
    // streaming event — the built DOM, the detail accessor, and the page/width it was
    // built for (so a page switch or resize correctly forces a full rebuild instead).
    area._lvRoot          = rootEl;
    area._lvSvg           = svgEl;
    area._lvGetDetail     = getNodeDetail;
    area._lvScaleWrap     = scaleWrap;
    area._lvRenderedIdx   = idx;
    area._lvRenderedWidth = availableWidth;

    if (savedScroll > 0) requestAnimationFrame(() => { area.scrollTop = savedScroll; });

    // Re-render on resize — observe parentElement, not area itself, to avoid feedback
    // loop where re-rendering area content triggers another resize observation.
    area._lvRo = new ResizeObserver(() => {
      clearTimeout(area._lvResizeTimer);
      area._lvResizeTimer = setTimeout(() => renderPage(currentPageIdx), 120);
    });
    if (area.parentElement) area._lvRo.observe(area.parentElement);
    else area._lvRo.observe(area);
  }

  // ── In-place live update (no DOM teardown) ──
  function updatePageLive(idx) {
    const area = getGraphArea();
    if (!area) return false;

    if (area._lvRenderedIdx !== idx || !area._lvRoot || !area.contains(area._lvRoot)) return false;

    const page = pages[idx];
    if (!page) return false;

    const sw = area._lvScaleWrap;
    if (sw) {
      const w = Math.max(300, sw.clientWidth || sw.offsetWidth || LOOP_W);
      if (Math.abs(w - (area._lvRenderedWidth || 0)) > 1) return false;
    }

    applyDiagramStates(area._lvRoot, area._lvSvg, page.nodeStates, {
      markerPrefix,
      getNodeDetail: area._lvGetDetail,
    });
    return true;
  }

  // Renders a static optimizer node (no click, no live state)
  function renderNodeEl(nodeDef, parent) {
    const node = document.createElement('div');
    node.className = `lv-node lv-type-${nodeDef.type}`;
    node.style.left   = (nodeDef.cx - nodeDef.hw) + 'px';
    node.style.top    = (nodeDef.cy - nodeDef.hh) + 'px';
    node.style.width  = (nodeDef.hw * 2) + 'px';
    node.style.height = (nodeDef.hh * 2) + 'px';

    const label = document.createElement('span');
    label.className = 'lv-node-label';
    label.textContent = nodeDef.label;
    node.appendChild(label);

    const detailEl = document.createElement('div');
    detailEl.className = 'lv-node-detail';
    if (nodeDef.desc) detailEl.textContent = nodeDef.desc;
    node.appendChild(detailEl);

    parent.appendChild(node);
  }

  // ── Show tool panel for a node ──
  function showToolPanel(nodeDef, nodeEl, container) {
    hideToolPanel();

    const page = pages[currentPageIdx];

    const panel = document.createElement('div');
    panel.className = 'lv-tool-panel lv-panel-overlay';

    const header = document.createElement('div');
    header.className = 'lv-tool-panel-header';

    const title = document.createElement('span');
    title.className = 'lv-tool-panel-title';
    title.textContent = nodeDef.label;
    header.appendChild(title);

    const close = document.createElement('button');
    close.className = 'lv-tool-panel-close';
    close.textContent = '✕';
    close.addEventListener('click', (e) => { e.stopPropagation(); hideToolPanel(); });
    header.appendChild(close);
    panel.appendChild(header);

    const info = NODE_PANEL_INFO[nodeDef.id];
    if (info) {
      const descLbl = document.createElement('div');
      descLbl.className = 'lv-tool-section-label';
      descLbl.textContent = 'What this does';
      panel.appendChild(descLbl);

      const descEl = document.createElement('div');
      descEl.className = 'lv-bp-meta';
      descEl.textContent = info.desc;
      panel.appendChild(descEl);

      if (info.details && info.details.length) {
        const detList = document.createElement('div');
        detList.className = 'lv-tool-panel-list';
        info.details.forEach(d => {
          const item = document.createElement('div');
          item.className = 'lv-tool-item';
          const nameEl = document.createElement('div');
          nameEl.className = 'lv-tool-name';
          nameEl.textContent = d.key;
          const valEl = document.createElement('div');
          valEl.className = 'lv-tool-desc';
          valEl.textContent = d.val;
          item.appendChild(nameEl);
          item.appendChild(valEl);
          detList.appendChild(item);
        });
        panel.appendChild(detList);
      }
    }

    // ── Build Prompt: raw LLM payload viewer ──
    if (nodeDef.id === 'build_prompt' && page) {
      const bpEvent = [...page.events].reverse().find(e => e.event.step === 'build_prompt');
      if (bpEvent) {
        const ev = bpEvent.event;
        const payloadLabel = document.createElement('div');
        payloadLabel.className = 'lv-tool-section-label';
        payloadLabel.textContent = 'LLM Payload';
        panel.appendChild(payloadLabel);

        const meta = document.createElement('div');
        meta.className = 'lv-bp-meta';
        const parts = [];
        if (ev.tool_count_in_prompt != null) parts.push(`${ev.tool_count_in_prompt} tools`);
        if (ev.brain_injected) parts.push('memory injected');
        if (ev.sections?.length) parts.push(`sections: ${ev.sections.join(', ')}`);
        meta.textContent = parts.join(' · ');
        panel.appendChild(meta);

        if (ev.system_prompt) {
          const pre = document.createElement('pre');
          pre.className = 'lv-bp-prompt';
          pre.textContent = ev.system_prompt;
          panel.appendChild(pre);
        }
      }
    }

    // ── Load Context: DB query info ──
    if (nodeDef.id === 'load_context') {
      const lcLabel = document.createElement('div');
      lcLabel.className = 'lv-tool-section-label';
      lcLabel.textContent = 'Query: agents table';
      panel.appendChild(lcLabel);
      const LC_COLS = ['system_prompt', 'agent_prompt', 'user_prompt', 'skills_prompt', 'tasks_prompt', 'misc_prompt'];
      const lcList = document.createElement('div');
      lcList.className = 'lv-tool-panel-list';
      LC_COLS.forEach(col => {
        const item = document.createElement('div');
        item.className = 'lv-tool-item';
        const name = document.createElement('div');
        name.className = 'lv-tool-name';
        name.textContent = col;
        item.appendChild(name);
        lcList.appendChild(item);
      });
      panel.appendChild(lcList);
    }

    // ── Build History: session history info ──
    if (nodeDef.id === 'build_history') {
      const atLabel = document.createElement('div');
      atLabel.className = 'lv-tool-section-label';
      atLabel.textContent = 'Session history';
      panel.appendChild(atLabel);
      const atDesc = document.createElement('div');
      atDesc.className = 'lv-bp-meta';
      atDesc.textContent = 'Loads prior interactions → OpenAI messages format. Strips internal tools (memory_search / memory_save). Rebuilds assistant tool_calls from persisted [Tool calls: …] suffix.';
      panel.appendChild(atDesc);
    }

    // ── Assemble Msgs: show messages structure ──
    if (nodeDef.id === 'assemble_msgs' && page) {
      const asLabel = document.createElement('div');
      asLabel.className = 'lv-tool-section-label';
      asLabel.textContent = 'messages[ ] structure';
      panel.appendChild(asLabel);
      const bpEvent = [...page.events].reverse().find(e => e.event && e.event.step === 'build_prompt');
      const sysSnippet = bpEvent ? (bpEvent.event.system_prompt || '').slice(0, 200) : '{ system_prompt }';
      const asPre = document.createElement('pre');
      asPre.className = 'lv-bp-prompt';
      asPre.textContent = `[0] system:\n${sysSnippet}${sysSnippet.length >= 200 ? '…' : ''}\n\n[1..N] { transcript }\n\n[N+1] { current user message }`;
      panel.appendChild(asPre);
    }

    // ── Load Tools: show tool count and names from pipeline event ──
    if (nodeDef.id === 'load_tools' && page) {
      const ltEvent = [...page.events].reverse().find(e => e.event && e.event.step === 'load_tools');
      const ltLabel = document.createElement('div');
      ltLabel.className = 'lv-tool-section-label';
      ltLabel.textContent = 'Tool registry loaded';
      panel.appendChild(ltLabel);
      if (ltEvent) {
        const ev = ltEvent.event;
        const meta = document.createElement('div');
        meta.className = 'lv-bp-meta';
        meta.textContent = `${ev.count ?? '?'} tools · ${ev.duration_ms ?? '?'}ms`;
        panel.appendChild(meta);
        if (Array.isArray(ev.names) && ev.names.length) {
          const pre = document.createElement('pre');
          pre.className = 'lv-bp-prompt';
          pre.style.maxHeight = '120px';
          pre.textContent = ev.names.join('\n');
          panel.appendChild(pre);
        }
      } else {
        const none = document.createElement('div');
        none.className = 'lv-tool-panel-empty';
        none.textContent = 'No load_tools event yet for this turn.';
        panel.appendChild(none);
      }
    }

    // ── Static items (slash commands + Settings shortcuts) ──
    const staticItems = NODE_STATIC_ITEMS[nodeDef.id] || [];
    if (staticItems.length > 0) {
      const lbl = document.createElement('div');
      lbl.className = 'lv-tool-section-label';
      lbl.textContent = 'Commands & Settings';
      panel.appendChild(lbl);

      const list = document.createElement('div');
      list.className = 'lv-tool-panel-list';
      staticItems.forEach(item => appendToolItem(list, item));
      panel.appendChild(list);
    }

    // ── Live tools section (derived from /admin/tools stage metadata) ──
    const toolsLabel = document.createElement('div');
    toolsLabel.className = 'lv-tool-section-label lv-tool-section-live';
    toolsLabel.innerHTML = 'Tools <span class="lv-live-dot"></span>';
    panel.appendChild(toolsLabel);

    const toolsList = document.createElement('div');
    toolsList.className = 'lv-tool-panel-list';

    const loadingEl = document.createElement('div');
    loadingEl.className = 'lv-tool-panel-empty lv-tool-loading';
    loadingEl.textContent = 'Loading…';
    toolsList.appendChild(loadingEl);
    panel.appendChild(toolsList);

    panel.addEventListener('click', e => e.stopPropagation());

    document.body.appendChild(panel);
    _activePanelNodeId = nodeDef.id;
    _activePanelEl = panel;

    setTimeout(() => {
      document.addEventListener('click', _outsideClickHandler, { once: true });
    }, 0);

    fetchAllToolMeta().then(allTools => {
      const nodeTools = allTools.filter(t => {
        const stages = _parseJsonField(t.stages, []);
        return stages.includes(nodeDef.id);
      });

      toolsList.innerHTML = '';

      if (nodeTools.length === 0) {
        const none = document.createElement('div');
        none.className = 'lv-tool-panel-empty';
        none.textContent = 'No tools mapped to this stage.';
        toolsList.appendChild(none);
        return;
      }

      nodeTools.sort((a, b) => {
        const aSkill = a.source === 'skill' ? 0 : 1;
        const bSkill = b.source === 'skill' ? 0 : 1;
        return aSkill - bSkill || a.name.localeCompare(b.name);
      });

      nodeTools.forEach(t => {
        const isDestructive = t.destructive === 1 || t.destructive === true;
        const isSkill = t.source === 'skill';
        const badge = isDestructive ? 'guarded' : isSkill ? 'skill' : 'tool';
        appendToolItem(toolsList, {
          name: t.name,
          type: badge,
          desc: t.description || '',
        });
      });
    });
  }

  function _outsideClickHandler() {
    hideToolPanel();
  }

  function hideToolPanel() {
    if (_activePanelEl) {
      _activePanelEl.remove();
      _activePanelEl = null;
    }
    _activePanelNodeId = null;
    document.removeEventListener('click', _outsideClickHandler);
  }

  // ── Page summary bar (shown at bottom of graph area) ──
  function updatePageSummary(idx) {
    const page = pages[idx];
    if (!page) return;

    const area = getGraphArea();
    if (!area) return;

    const doneCount  = [...page.nodeStates.values()].filter(s => s === 'done').length;
    const errorCount = [...page.nodeStates.values()].filter(s => s === 'error').length;
    const totalCount = LOOP_NODES.length;

    let summary = area.querySelector(':scope > .lv-page-summary');
    if (!summary) {
      summary = document.createElement('div');
      summary.className = 'lv-page-summary';
      area.appendChild(summary);
    }

    const html =
      `<span><span class="lv-summary-label">Turn</span> ${page.turnNum}</span>` +
      `<span><span class="lv-summary-label">Steps</span> ${doneCount}/${totalCount}</span>` +
      (errorCount > 0 ? `<span style="color:#f7768e;"><span class="lv-summary-label" style="color:#f7768e;">Errors</span> ${errorCount}</span>` : '') +
      `<span><span class="lv-summary-label">Events</span> ${page.events.length}</span>`;
    if (summary._lvHtml !== html) {
      summary.innerHTML = html;
      summary._lvHtml = html;
    }
  }

  // ── Fetch session history from DB ──
  async function fetchLoopHistory() {
    const area = getGraphArea();
    try {
      const rows = await fetchInteractionRows();
      if (!rows) { replayBuffer(); return; }

      sessionLoaded = true;
      rows.forEach(row => {
        const events = interactionToEvents(row);
        events.forEach(ev => processEvent(ev));
      });

      replayBuffer();

      if (pages.length === 0 && area) {
        area.innerHTML = `<div class="loop-visual-hint">${emptyHint}</div>`;
      }
    } catch (e) {
      console.error('[loop-visual] fetch failed:', e);
      replayBuffer();
    }
  }

  function replayBuffer() {
    const area = getGraphArea();
    const hint = area?.querySelector('.loop-visual-hint');
    if (hint && eventBuffer.length > 0) hint.remove();

    eventBuffer.forEach(ev => processEvent(ev));
    eventBuffer = [];

    if (pages.length === 0 && area) {
      area.innerHTML = `<div class="loop-visual-hint">${emptyHint}</div>`;
    }
  }

  // ── Lifecycle ──
  function start() {
    active = true;
    if (!isAlive()) { destroy(); return; }
    if (!gate()) { if (onBlocked) onBlocked(); return; }

    _resetState();

    const area = getGraphArea();
    const pagesEl = getPagesEl();
    if (!area) return;

    area.innerHTML = '<div class="loop-visual-hint">Loading loop history…</div>';
    if (pagesEl) pagesEl.innerHTML = '';
    renderSidebar();

    fetchLoopHistory();
  }

  function stop() {
    active = false;
  }

  function sessionChanged() {
    if (!active) return;
    if (!isAlive()) { destroy(); return; }
    if (!gate()) { _resetState(); eventBuffer = []; if (onBlocked) onBlocked(); return; }

    const area = getGraphArea();
    if (area) area.innerHTML = '<div class="loop-visual-hint">Session changed — reloading…</div>';

    _resetState();
    eventBuffer = [];

    fetchLoopHistory();
    renderSidebar();
  }

  // ── Sidebar (run-history scrubber + node directory) ──
  function renderRunHistoryList() {
    const host = getSidebar()?.pages;
    if (!host) return;
    if (!pages.length) {
      host.innerHTML = '<div class="rl-empty">No runs yet</div>';
      host._rlCount = 0;
      return;
    }
    if (host._rlCount === pages.length) {
      pages.forEach((p, i) => {
        const row = host.querySelector('.rl-page-row[data-idx="' + i + '"]');
        if (!row) return;
        row.classList.toggle('active', i === currentPageIdx);
        const meta = row.querySelector('.rl-row-meta');
        const txt  = (p.events ? p.events.length : 0) + ' ev · ' + _rlRelativeTime(p.ts);
        if (meta && meta.textContent !== txt) meta.textContent = txt;
      });
      return;
    }
    host.innerHTML = '';
    host._rlCount = pages.length;
    pages.forEach((p, i) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'rl-row rl-page-row' + (i === currentPageIdx ? ' active' : '');
      row.dataset.idx = String(i);
      const label = p.turnNum <= 1 ? 'Initial' : ('Turn ' + (p.turnNum - 1));
      row.innerHTML =
        '<span class="rl-row-num">#' + _rlEscape(String(p.turnNum)) + '</span>' +
        '<span class="rl-row-label">' + _rlEscape(label) + '</span>' +
        '<span class="rl-row-meta">' + (p.events ? p.events.length : 0) + ' ev · ' + _rlEscape(_rlRelativeTime(p.ts)) + '</span>';
      row.addEventListener('click', () => selectPage(i));
      host.appendChild(row);
    });
  }

  function renderNodesList() {
    const host = getSidebar()?.nodes;
    if (!host) return;
    const page = pages[currentPageIdx];
    const stateMap = (page && page.nodeStates) || currentNodeStates;

    if (host._rlNodesBuilt) {
      for (const nd of LOOP_NODES) {
        const state = (stateMap && stateMap.get(nd.id)) || 'idle';
        const row = host.querySelector('.rl-node-row[data-node-id="' +
          (window.CSS && CSS.escape ? CSS.escape(nd.id) : nd.id) + '"]');
        if (!row) continue;
        row.className = 'rl-row rl-node-row rl-state-' + _rlEscape(state);
        const dot = row.querySelector('.rl-node-dot');
        if (dot) dot.setAttribute('data-state', state);
        const meta = row.querySelector('.rl-row-meta');
        if (meta && meta.textContent !== state) meta.textContent = state;
      }
      return;
    }

    host.innerHTML = '';
    host._rlNodesBuilt = true;
    for (const nd of LOOP_NODES) {
      const state = (stateMap && stateMap.get(nd.id)) || 'idle';
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'rl-row rl-node-row rl-state-' + _rlEscape(state);
      row.dataset.nodeId = nd.id;
      row.innerHTML =
        '<span class="rl-node-dot" data-state="' + _rlEscape(state) + '"></span>' +
        '<span class="rl-row-label">' + _rlEscape(nd.label) + '</span>' +
        '<span class="rl-row-meta">' + _rlEscape(state) + '</span>';
      row.addEventListener('click', () => _focusNode(nd.id));
      host.appendChild(row);
    }
  }

  function _focusNode(nodeId) {
    const root = getGraphArea();
    if (!root) return;
    const el = root.querySelector('.lv-node[data-id="' + (window.CSS && CSS.escape ? CSS.escape(nodeId) : nodeId) + '"]');
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
    }
    const nodeDef = LOOP_NODES.find((n) => n.id === nodeId);
    if (!nodeDef) return;
    if (_activePanelNodeId === nodeId) hideToolPanel();
    else showToolPanel(nodeDef, el, root);
  }

  function renderSidebar() {
    const sb = getSidebar();
    if (!sb || (!sb.pages && !sb.nodes)) return;
    renderRunHistoryList();
    renderNodesList();
    const btn = sb.refresh;
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = '1';
      btn.addEventListener('click', () => { try { start(); } catch (_) {} });
    }
  }

  function destroy() {
    active = false;
    hideToolPanel();
    _views.delete(api);
  }

  const api = { start, stop, sessionChanged, handleEvent, renderSidebar, destroy };
  _views.add(api);
  return api;
}

// ── Relative-time helpers (shared by the sidebar lists) ──
function _rlEscape(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _rlRelativeTime(ts) {
  if (!ts) return '';
  const dt = Date.now() - ts;
  if (dt < 0) return 'now';
  const s = Math.floor(dt / 1000);
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

// ═════════════════════════════════════════════════════════════════════════════
//  Admin (secondary) view + back-compat module API
// ═════════════════════════════════════════════════════════════════════════════
// The admin Runtime Loop page and the rest of the app call these module-level
// functions. They operate the single admin view (bound to the legacy DOM ids)
// and fan session-changes out to EVERY live view (admin + agent-tab mounts).

let _adminView = null;
function _ensureAdminView() {
  if (!_adminView) _adminView = createLoopView({});   // defaults bind to admin ids
  return _adminView;
}

// ── Init — register the WS fan-out handler ──
export function initLoopVisual() {
  app._loopVisualHandler = (event) => {
    for (const v of [..._views]) {
      try { v.handleEvent(event); } catch (_) { /* ignore */ }
    }
  };
}

export function startLoopVisual()        { _ensureAdminView().start(); }
export function stopLoopVisual()         { if (_adminView) _adminView.stop(); }
export function renderRuntimeLoopSidebar(){ _ensureAdminView().renderSidebar(); }

// Session changed (called from session-init/core + optimizer-stats). Fan out to
// every live view; each re-checks its own gate and reloads or shows its blueprint.
export function loopVisualSessionChanged() {
  for (const v of [..._views]) {
    try { v.sessionChanged(); } catch (_) { /* ignore */ }
  }
}

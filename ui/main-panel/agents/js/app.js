'use strict';

/**
 * Agents — application orchestrator.
 *
 * Exports the public API: initAgents, startAgents, stopAgents.
 * Wires the top-level init flow, agent builder bar, and view state restoration.
 */

import { app } from '../../../shared/js/state.js';
import { routeParam } from '../../../shared/js/url-state.js';
import {
  _agents, _expandedAgents, _setActive,
  MOCK_AGENT_ID, _isMockAgent, _setAgentRunning,
} from './state.js';
import {
  _loadProfile, _loadAgents, _loadAppSettings,
  primeAgentDataFromCache, hydrateAgentDataCache,
} from './data.js';
import {
  _renderSkeleton, _renderList,
  _bindBinToolbar, _updateBinToolbar,
  _restoreViewState, _applyAgentRunStatus,
  _loadViewType,
} from './view.js?v=783';
import { startSessions, stopSessions } from '../sessions/js/sessions-page.js';
import { stopAutomations } from '../automations/js/automations.js';
import { invalidateAgentListCaches } from './agent-cache.js';
import { mountAgentsPageChatWidget, unmountAgentsPageChatWidget } from '../../../shared/js/agents-page-chat-widget.js';

function _markAgentsPaint(source) {
  const grid = document.getElementById('agents-grid');
  if (!grid || typeof performance === 'undefined') return;
  const at = performance.now().toFixed(1);
  grid.dataset.lastPaintAt = at;
  grid.dataset.lastPaintSource = source;
  if (!grid.dataset.firstPaintAt) {
    grid.dataset.firstPaintAt = at;
    grid.dataset.firstPaintSource = source;
  }
}

function _agentsPageIsActive() {
  return document.getElementById('tab-agents')?.classList.contains('active') === true;
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function initAgents() {
  if (!app.currentUserId) return;

  if (!_agentsPageIsActive()) return;

  // Resolve the viewport-driven view type (cards on desktop / list on mobile)
  // before the first render so the page never flashes the wrong layout. This
  // also wires the media-query listener that flips the view on resize.
  _loadViewType();

  // First paint from the tenant-scoped IndexedDB row (or the shared in-memory
  // agent plane) before waiting for profile/settings/agent API requests.
  const primed = primeAgentDataFromCache();
  if (primed && _agents.length) {
    _renderList();
    _markAgentsPaint('memory');
    _bindBinToolbar();
    _updateBinToolbar();
    _restoreViewState();
  } else if (_agents.length === 0) {
    _renderSkeleton();
  }

  let liveDone = false;
  hydrateAgentDataCache().then((applied) => {
    if (!liveDone && applied && _agents.length) {
      _renderList();
      _markAgentsPaint('indexeddb');
      _bindBinToolbar();
      _updateBinToolbar();
      _restoreViewState();
    }
  });

  // The roster is the only request that gates replacing the square skeleton.
  // Profile and app settings are auxiliary metadata; waiting for them here made
  // one slow/stalled settings request leave the agent strip shimmering forever
  // even though /agents had already returned (the independently-started Sessions
  // table would still finish, producing a visibly half-loaded page).
  const auxiliaryLoads = Promise.allSettled([_loadProfile(), _loadAppSettings()]);
  await _loadAgents();
  const routeAgent = routeParam('agent');
  const routeView = routeParam('agentView');
  if (routeAgent && _agents.some((agent) => agent.id === routeAgent)) _setActive(routeAgent, routeView || 'model');
  liveDone = true;
  _renderList();
  _markAgentsPaint('network');
  _bindBinToolbar();
  _updateBinToolbar();
  _restoreViewState();

  // Apply admin/settings-dependent controls once their requests settle. This is
  // a metadata refresh only: the real squares are already visible, so auxiliary
  // failures can no longer strand the page in its initial skeleton state.
  await auxiliaryLoads;
  if (document.getElementById('agents-grid') && _agentsPageIsActive()) {
    _renderList();
    _bindBinToolbar();
    _updateBinToolbar();
    _restoreViewState();
  }
}

export function startAgents() {
  if (!app.currentUserId) return;
  // Route-scoped launcher: it is a sibling overlay in #tab-agents, never part
  // of an ability table or the scrolling Agents content.
  mountAgentsPageChatWidget();
  initAgents();
  // The Agents page embeds the shared Sessions table in the All Agents card —
  // start the sessions engine alongside the agents render (it is idempotent and
  // re-renders fresh data on each activation).
  startSessions();
}

export function stopAgents() {
  // The launcher belongs only to the Agents route; an already-open task widget
  // remains available as a normal shared chat-widget session.
  unmountAgentsPageChatWidget();
  // Stop the embedded sessions engine's auto-refresh + relative-time tickers.
  stopSessions();
  // Stop the embedded automations engine's auto-refresh if its tab is open
  // (startAutomations is driven lazily by the All Agents card tab in view.js).
  stopAutomations();
}

// ── Global expand handler (called from chat-header dropdown) ──────────────────

window.expandAgent = function expandAgent(agentId) {
  if (!agentId) return;
  _setActive(agentId);
  let attempts = 0;
  const tryRender = () => {
    attempts += 1;
    if (_agents.find(a => a.id === agentId)) {
      _renderList();
      requestAnimationFrame(() => {
        const region = document.getElementById('agents-detail-region');
        if (region && region.scrollIntoView) region.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      return;
    }
    if (attempts < 40) setTimeout(tryRender, 50);
  };
  tryRender();
};

// ── Refresh handler (called from chat-header after reorder) ───────────────────

app.refreshAgentsOrder = async function refreshAgentsOrder() {
  if (!app.currentUserId) return;
  if (!document.getElementById('agents-grid')) return;
  await _loadAgents();
  const routeAgent = routeParam('agent');
  const routeView = routeParam('agentView');
  if (routeAgent && _agents.some((agent) => agent.id === routeAgent)) _setActive(routeAgent, routeView || 'model');
  _renderList();
};

// ── Live agent lifecycle/status handler (called from WebSocket) ───────────────
// Fired when an agent is created / trashed / permanently deleted / restored in
// ANOTHER tab or device, or starts/stops a run. Keeps the grid in sync without a
// manual refresh. Lifecycle changes are infrequent, so we reload+rerender (the
// same primitive refreshAgentsOrder uses) — correct for every view (main / bin /
// clones / system) without per-view surgical branching. Status changes are
// frequent, so they only repaint the affected card's dot.
app.onAgentLifecycleEvent = async function onAgentLifecycleEvent(event) {
  if (!event || !app.currentUserId) return;

  const gridMounted = !!document.getElementById('agents-grid');

  if (event.type === 'agent_status') {
    // Status dots only matter when the grid is actually on screen.
    if (!gridMounted) return;
    const changed = _setAgentRunning(event.agent_id, event.session_id, event.status === 'running');
    if (changed) _applyAgentRunStatus(event.agent_id);
    return;
  }

  // Lifecycle: created / trashed / deleted / restored. ALWAYS invalidate the
  // cross-page cache so the grid is correct the next time it's opened — even if
  // the Agents page isn't mounted right now (e.g. the user is in chat while an
  // agent with agent-management abilities creates a new agent). Only reload +
  // rerender when the grid is on screen.
  window.__agentsSharedData = null;
  invalidateAgentListCaches();
  if (!gridMounted) return;
  await _loadAgents();
  const routeAgent = routeParam('agent');
  const routeView = routeParam('agentView');
  if (routeAgent && _agents.some((agent) => agent.id === routeAgent)) _setActive(routeAgent, routeView || 'model');
  _renderList();
  _updateBinToolbar();
};

'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — view rendering: grid, squares carousel, agent card, tab bar, detail panel.
 *
 * Combined into one module to avoid circular dependencies between grid rendering
 * and agent card rendering (which call each other via _selectAgent ↔ _renderAgentCard).
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders, isAnonGuest, showLeftOverlay } from '../../../shared/js/left-login.js';
import { icon, claudeMark, codexMark } from '../../../shared/js/icons.js';
import { sortAgentsForDisplay } from '../../../shared/js/ordering.js';
import { kvRead, kvWrite } from '../../../shared/js/kv-ui-state.js';
import { invalidateAgentListCaches } from './agent-cache.js';
import {
  _agents, _expandedAgents, _activeId, _setActive,
  _isMockAgent, _createMockAgent, MOCK_AGENT_ID,
  _binView, _clonesView, _selectedIds,
  _clearExpanded, _setAgents,
  _isAgentRunning,
  _ensureAbilityCatalog,
  TIER_2_CATEGORIES,
  _localLoopLogicObjs, _loopNodeEnabledPersisted,
  _mockAgentConfigPayload,
} from './state.js';
import { _loadProfile, _loadAgents, _loadAppSettings, _fetchAbilitiesAndTools, _fetchMembersCount } from './data.js';
import {
  _esc, _btn, _timeAgo, _displayName, _iconColor,
  _renderAgentIcon, _makeAutosaveCheck, _flashSaved, _markSaving,
  _debounced, _putAgentField, _toggleSave, _triggerKeyPlaceholder,
} from './utils.js';
import {
  _mockRandomIcon, _wireMockCreateField, _postNewAgent,
  _mockCreateType, _setMockCreateType, _mockDraftName, _mockEngineDraft,
  _mockAbilityConnections,
} from './mock-agent.js';
import { _openIconPicker } from './icon-picker.js';
// Local Claude Code is its OWN engine: the "Claude" segment of the unified create
// card builds renderClaudeCreateBody, and a created claude_code agent opens into a
// stripped, tab-less card. See ui/main-panel/agents/js/claude-agent.js.
import { _isClaudeAgent, mountClaudeCardTabs, renderClaudeCreateBody, _defaultClaudeName } from './claude-agent.js';
import { _isCodexAgent, renderCodexCreateBody, renderCodexSettings, _defaultCodexName, mountCodexCardTabs } from './codex-agent.js';
// Terminal Chat is its OWN engine: the "Terminal" segment of the create card builds
// renderTerminalChatCreateBody. See ui/main-panel/agents/js/terminal-chat-agent.js.
import { _isTerminalChatAgent, mountTerminalChatCardTabs, renderTerminalChatCreateBody, _defaultTerminalChatName } from './terminal-chat-agent.js';
import { setSessionsAgentContext } from '../pages/sessions/page.js';
// The All Agents card's Automations tab hosts the shared automations dashboard
// (ui/main-panel/agents/automations/). Started lazily when the tab opens, stopped
// when the tab closes or the page deactivates — unlike sessions, which runs for
// the whole page lifetime.
import { startAutomations, stopAutomations } from '../automations/js/automations.js';
import { apiPath } from '../../../shared/js/config.js';
let mountUserModelCatalog;
let initModelAnalytics;
import agentPagesCatalog from '../pages/catalog.json' with { type: 'json' };
import { replaceRouteParams } from '../../../shared/js/url-state.js';
import { readAgentPageFavorites, toggleAgentPageFavorite, writeAgentPageFavorites } from '../pages/shared/page-favorites.js';
import { readMobileAgentFavorites, toggleMobileAgentFavorite } from '../pages/shared/agent-favorites.js';
// The My Agents (All Agents) card mirrors the per-agent Config directory, but its
// page ids differ (`models` is not a per-agent catalog id), so it carries its own
// catalog and its own server-backed favorites record. See my-pages/.
import myPagesCatalog from '../my-pages/catalog.json' with { type: 'json' };
import {
  readMyAgentsPageFavorites, toggleMyAgentsPageFavorite, writeMyAgentsPageFavorites,
} from '../my-pages/shared/page-favorites.js';

const _CONFIG_TOPIC_PAGES = Object.freeze(
  (agentPagesCatalog.pages || [])
    .filter((page) => page?.kind === 'config-topic')
    .sort((left, right) => (left.order || 0) - (right.order || 0)),
);
const _CONFIG_TOPIC_IDS = new Set(_CONFIG_TOPIC_PAGES.map((page) => page.id));

// ── Regster window-level callbacks for surgical updates ───────────────────────
// These let utility/mock-agent modules trigger re-renders without circular imports.
window.__agentsSurgicalUpdateSquare = _surgicalUpdateSquare;
window.__agentsSurgicalAddSquare = _surgicalAddSquare;
window.__agentsRebuildDetailRegion = _rebuildDetailRegion;
// A remote page-preference snapshot may differ from a stale browser mirror.
// Refresh cards only for that server-driven reconciliation, never for a local star click.
window.addEventListener('agent-page-favorites-hydrated', () => {
  if (_activeId) _renderList();
});
// Same reconciliation for the My Agents (All Agents) header order. That card is
// shown only when no agent is expanded, so repaint under that condition.
window.addEventListener('my-agents-page-favorites-hydrated', () => {
  if (!_activeId) _renderList();
});
// Full reload+repaint of the grid — used by the Claude card's in-panel delete
// (claude-agent.js) to return to the overview after a soft-delete.
window.__agentsReload = async () => {
  window.__agentsSharedData = null;
  invalidateAgentListCaches();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
};

// ── View type (cards / list) ──────────────────────────────────────────────────
// The Agents overview has two view types: CARDS (wide view) and LIST (narrow
// view). The active one is chosen automatically from the width of the MAIN PANEL
// view that holds the agents page — NOT from the browser window. On a large
// desktop the window can be wide while the main panel is dragged narrow; the
// page must follow the panel, so cards render when the view is >=500px and the
// list when it is narrower. The width is measured from #main-panel (the panel
// view, resized via #chat-resize-handle) through a ResizeObserver, so the view
// flips live as the panel resizes. There is no manual toggle. List view only
// applies to the normal overview — bin and clones always stay in cards (see
// _renderList).
let _viewType = 'cards';

const _VIEW_BREAKPOINT = 500;

function _viewContainer() {
  return document.getElementById('main-panel') || document.getElementById('agents-grid');
}

function _viewWidth() {
  const el = _viewContainer();
  return el ? el.clientWidth : 0;
}

function _resolveViewType() {
  return _viewWidth() >= _VIEW_BREAKPOINT ? 'cards' : 'list';
}

function _applyViewType() {
  const next = _resolveViewType();
  if (next === _viewType) return;
  _viewType = next;
  // Only re-render when the Agents tab is actually on screen; otherwise just
  // record the new view type so the page is correct the next time it opens.
  const agentsActive =
    document.getElementById('tab-agents')?.classList.contains('active') === true;
  if (agentsActive && document.getElementById('agents-grid')) _renderList();
}

export function _loadViewType() {
  _viewType = _resolveViewType();
  // Re-evaluate whenever the main panel view is resized — live panel drag, the
  // chat panel being docked/collapsed, or the window itself — so the overview
  // flips between cards and list without user action.
  if (document._agentsViewReady) return;
  document._agentsViewReady = true;
  const el = _viewContainer();
  if (el && typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => _applyViewType());
    ro.observe(el);
  } else if (typeof window.addEventListener === 'function') {
    window.addEventListener('resize', _applyViewType); // fallback
  }
}



// ── Recycling-bin notice toast ────────────────────────────────────────────────
// A brief fixed toast (mirrors the Sessions/automations delete toasts) with an
// optional action button. Used by the Config tab's delete row (tab-config.js) to
// tell the user the agent — and all of its sessions — are in the recycling bin.
let _agentsNoticeTimer = null;
function _hideAgentsNotice(toast) {
  if (!toast) toast = document.getElementById('agents-notice-toast');
  if (!toast) return;
  toast.classList.remove('agents-notice-visible');
  clearTimeout(_agentsNoticeTimer);
}
window.__agentsNotice = (msg, opts = {}) => {
  let toast = document.getElementById('agents-notice-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'agents-notice-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    document.body.appendChild(toast);
  }
  toast.innerHTML = '';
  const text = document.createElement('span');
  text.textContent = msg;
  toast.appendChild(text);
  if (opts.action && typeof opts.action.fn === 'function') {
    const act = document.createElement('button');
    act.type = 'button';
    act.className = 'agents-notice-action';
    act.textContent = opts.action.label || 'Open';
    act.addEventListener('click', () => { opts.action.fn(); _hideAgentsNotice(toast); });
    toast.appendChild(act);
  }
  toast.classList.toggle('agents-notice-error', opts.kind === 'error');
  toast.classList.add('agents-notice-visible');
  clearTimeout(_agentsNoticeTimer);
  _agentsNoticeTimer = setTimeout(() => _hideAgentsNotice(toast), 4000);
};
// Lets the toast's "Open bin" action jump into the bin view (module-private here).
window.__agentsOpenBin = () => _enterBin();

// ── Skeleton ──────────────────────────────────────────────────────────────────

export function _renderSkeleton(count) {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  // Exactly two skeleton squares (matching the static phantom — see agents.html).
  // Each card's lines still get a fresh random width so every load paints a
  // slightly different skeleton.
  const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  const n = (typeof count === 'number') ? count : 2;
  let html = '';
  for (let i = 0; i < n; i++) {
    const w1 = rand(55, 92), w2 = rand(30, 50);
    html += `
      <div class="agent-row">
        <div class="agent-skeleton" aria-hidden="true">
          <div class="sk-icon sk-shimmer"></div>
          <div class="sk-lines">
            <div class="sk-line sk-shimmer" style="width:${w1}%"></div>
            <div class="sk-line sk-shimmer" style="width:${w2}%"></div>
          </div>
        </div>
      </div>`;
  }
  grid.classList.remove('carousel');
  // Keep Back/Restore hidden until the live list is rendered.
  grid.classList.add('agents-skeleton');
  // Preserve the toolbar — _renderSkeleton is called before _renderList, and
  // _renderList saves+re-inserts the toolbar, but if we wipe it here with
  // innerHTML the toolbar is gone for good. Same pattern as _renderList.
  const toolbar = document.getElementById('agents-toolbar');
  // Same wrap structure as _renderList so the skeleton carousel sits exactly
  // where the live squares will (the merged layout styles the WRAP, not the
  // grid — no wrap means the row would hug the top). The toolbar is re-inserted
  // in the same top row for Back/Restore actions after the live render.
  grid.innerHTML = `
    <div class="agents-top-row">
      <div class="agents-squares-wrap">
        <button type="button" class="agents-carousel-chev left" aria-label="Scroll agents left" tabindex="-1">&#10094;</button>
        <button type="button" class="agents-carousel-chev right" aria-label="Scroll agents right" tabindex="-1">&#10095;</button>
        <div class="agents-squares">${html}</div>
      </div>
    </div>`;
  const topRow = grid.querySelector('.agents-top-row');
  if (topRow && toolbar) topRow.appendChild(toolbar);
  _syncMergedLayout();
}

// ── Main render ───────────────────────────────────────────────────────────────

export function _renderList() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;

  // List view replaces the cards carousel for the normal overview only. The bin
  // and clones views always stay in cards so their dedicated layouts are intact.
  if (_viewType === 'list' && !_binView && !_clonesView) {
    _renderListView();
    return;
  }

  // The Sessions tab may currently own #sessions-section. Move it out before
  // rebuilding the grid so the single table instance is never destroyed.
  _destroyAllAgentsSessionsShell();
  setSessionsAgentContext(null);
  // Same for the Automations tab — move #automations-section back to the page
  // and stop its engine before the rebuild wipes the card body.
  _setAutomationsContext(null);
  stopAutomations();
  _destroyAllAgentsModelTable();

  const activeId = _activeId();
  const allAgentsActive = !_binView && !_clonesView && !activeId;
  grid.classList.toggle('carousel', !!activeId || allAgentsActive);
  // Leaving the list view must drop its layout marker so the cards carousel
  // (and the merged-overview CSS) don't inherit the list styling.
  grid.classList.remove('agents-list-view');
  // Skeleton state ends here: the real toolbar (hidden by the .agents-skeleton
  // rules) becomes visible again, and the shimmer stand-in is gone with the
  // rebuild below.
  grid.classList.remove('agents-skeleton');

  // Preserve scroll position across the wholesale rebuild below. #agents-grid is
  // the vertical scroller in carousel mode (its own scrollTop zeroes when its
  // innerHTML is cleared), and .agents-squares is REBUILT as a brand-new node each
  // render (so its scrollTop in grid mode / scrollLeft in carousel mode start at
  // 0). Capture both from the outgoing nodes now and restore onto the fresh ones
  // after — otherwise a background WS refresh or opening a tile snaps the page to
  // the top / the tile row back to the left.
  const _prevGridTop = grid.scrollTop;
  const _oldSquares = grid.querySelector('.agents-squares');
  const _prevSqTop = _oldSquares ? _oldSquares.scrollTop : 0;
  const _prevSqLeft = _oldSquares ? _oldSquares.scrollLeft : 0;

  // Save the toolbar so it can be re-inserted after rebuild — it sits to the
  // RIGHT of the squares strip as a vertical action stack (same top row).
  const toolbar = document.getElementById('agents-toolbar');

  grid.innerHTML = '';

  // Top row: squares strip + toolbar. Flex row — the strip flexes to fill,
  // the toolbar is a fixed-width vertical stack on the right.
  const topRow = document.createElement('div');
  topRow.className = 'agents-top-row';

  // Squares strip
  const wrap = document.createElement('div');
  wrap.className = 'agents-squares-wrap';
  wrap.innerHTML = `
    <button type="button" class="agents-carousel-chev left" aria-label="Scroll agents left" tabindex="-1">&#10094;</button>
    <button type="button" class="agents-carousel-chev right" aria-label="Scroll agents right" tabindex="-1">&#10095;</button>`;
  const squares = document.createElement('div');
  squares.className = 'agents-squares';
  wrap.appendChild(squares);
  topRow.appendChild(wrap);

  // Re-insert the toolbar as the last child of the top row (right side)
  if (toolbar) topRow.appendChild(toolbar);

  grid.appendChild(topRow);

  // New Agent tile (hidden in bin/clones)
  const mockAgent = _createMockAgent();
  let activeAgent = (!_binView && !_clonesView && activeId === MOCK_AGENT_ID) ? mockAgent : null;

  if (!_binView && !_clonesView) {
    _renderAllAgentsSquare(squares, allAgentsActive);
    // NOTE — there is no dedicated "New Agent" tile anymore: creation lives on
    // the All Agents square's corner + button (see _renderAllAgentsSquare).
  }

  if (_agents.length) {
    if (_clonesView) {
      _renderClonesList(squares);
    } else {
      const ordered = sortAgentsForDisplay(_agents, app.currentUserId);
      for (const agent of ordered) {
        const isActive = activeId === agent.id;
        _renderSquare(squares, agent, isActive);
        if (isActive) activeAgent = agent;
      }
    }
  } else if (_binView) {
    const empty = document.createElement('div');
    empty.className = 'agents-bin-empty';
    empty.textContent = 'The recycling bin is empty.';
    squares.appendChild(empty);
  } else if (_clonesView) {
    const empty = document.createElement('div');
    empty.className = 'agents-bin-empty';
    empty.textContent = 'No clones.';
    squares.appendChild(empty);
  }

  // Detail region
  const region = document.createElement('div');
  region.id = 'agents-detail-region';
  grid.appendChild(region);
  if (allAgentsActive) {
    region.classList.add('open');
    _renderAllAgentsCard(region);
  } else if (activeAgent) {
    region.classList.add('open');
    _renderAgentCard(region, activeAgent);
  }

  // Restore the pre-rebuild scroll offsets onto the freshly-created nodes (see the
  // capture near the top of _renderList). `squares` is the new .agents-squares.
  if (_prevGridTop) grid.scrollTop = _prevGridTop;
  if (squares) {
    if (_prevSqTop) squares.scrollTop = _prevSqTop;
    if (_prevSqLeft) squares.scrollLeft = _prevSqLeft;
  }

  _wireSquaresCarousel(wrap);
  _updateBinToolbar();
  _syncMergedLayout();
}

// ── Square rendering ──────────────────────────────────────────────────────────

function _renderAllAgentsSquare(container, isActive) {
  const card = document.createElement('div');
  card.className = 'agent-card agent-square agent-square-all' + (isActive ? ' activated' : '');
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap color-purple">${icon('layout-grid', { size: '24px' })}</div>
      <div class="agent-card-meta"><div class="agent-card-name-row">
        <span class="agent-card-name">My Agents</span>
      </div></div>
    </div>`;
  card.addEventListener('click', () => {
    _clearExpanded();
    setSessionsAgentContext(null);
    _setAutomationsContext(null);
    stopAutomations();
    _renderList();
    _saveViewState();
  });
  // "New agent" + button (bottom-right). The dedicated "New Agent" tile was
  // folded into this square, so creation now lives here: this opens the SAME
  // create card the tile did (activeId → MOCK_AGENT_ID → the mock create card
  // renders in the detail region). Mirrors the .agent-square-chat-btn overlay.
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'agent-square-add-btn';
  addBtn.title = 'Create a new agent';
  addBtn.setAttribute('aria-label', 'Create a new agent');
  addBtn.innerHTML = icon('plus', { size: '14px' });
  addBtn.addEventListener('click', e => {
    e.stopPropagation();
    _selectAgent(_createMockAgent());
  });
  card.appendChild(addBtn);
  const row = document.createElement('div');
  row.className = 'agent-row';
  row.dataset.view = 'all-agents';
  row.appendChild(card);
  container.appendChild(row);
}

// The account-wide card mirrors a normal expanded agent card. Sessions,
// automations, and abilities are account-wide; app admins also receive the
// global model roster between Automations and Abilities.
let _allAgentsTab = 'models';
let _allAgentsModelTable = null;
let _allAgentsModelAnalytics = null;
let _allAgentsSessionsShellCleanup = null;

function _setAllAgentsSessionsFooterDock(active) {
  document.getElementById('tab-agents')?.classList.toggle('agents-sessions-footer-docked', active);
}

function _destroyAllAgentsSessionsShell() {
  if (_allAgentsSessionsShellCleanup) _allAgentsSessionsShellCleanup();
  _allAgentsSessionsShellCleanup = null;
  _setAllAgentsSessionsFooterDock(false);
}

// The desktop Sessions shell fills the visible grid only after the carousel has
// scrolled away. Keeping these two values measured—not viewport-derived—makes
// the sticky panel land exactly beneath the tab card across app chrome sizes.
function _mountAllAgentsSessionsShell(body) {
  _destroyAllAgentsSessionsShell();
  const grid = document.getElementById('agents-grid');
  const row = body?.closest('.agent-row-all-agents');
  const card = row?.querySelector('.agent-card-tabs-only');
  if (!grid || !row || !card) return;

  const desktop = window.matchMedia('(min-width: 801px)');
  // The Sessions footer follows the Instances Dashboard pattern: it is a
  // page-level sibling dock on every breakpoint, while only the measured sticky
  // panel sizing below is desktop-specific.
  _setAllAgentsSessionsFooterDock(true);
  if (!desktop.matches) return;
  let frame = 0;
  const sync = () => {
    frame = 0;
    if (!body.isConnected || !grid.isConnected || !card.isConnected) return;
    if (!desktop.matches) {
      _setAllAgentsSessionsFooterDock(false);
      return;
    }
    _setAllAgentsSessionsFooterDock(true);
    const gridHeight = Math.round(grid.clientHeight);
    const tabsHeight = Math.round(card.getBoundingClientRect().height);
    if (gridHeight <= 0 || tabsHeight <= 0 || gridHeight <= tabsHeight) return;
    grid.style.setProperty('--all-agents-sessions-scrollport-height', `${gridHeight}px`);
    grid.style.setProperty('--all-agents-sessions-tabs-height', `${tabsHeight}px`);
  };
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(sync);
  };
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
  observer?.observe(grid);
  observer?.observe(card);
  window.addEventListener('resize', schedule);
  schedule();

  _allAgentsSessionsShellCleanup = () => {
    if (frame) cancelAnimationFrame(frame);
    observer?.disconnect();
    window.removeEventListener('resize', schedule);
    grid.style.removeProperty('--all-agents-sessions-scrollport-height');
    grid.style.removeProperty('--all-agents-sessions-tabs-height');
  };
}

function _destroyAllAgentsModelTable() {
  _allAgentsModelAnalytics?.destroy();
  _allAgentsModelAnalytics = null;
  if (!_allAgentsModelTable) return;
  _allAgentsModelTable.destroy();
  _allAgentsModelTable = null;
}

// The All Agents Automations tab owns the shared automations section (see
// agents.html #automations-section). Moving the SAME DOM node into the card —
// mirroring setSessionsAgentContext for #sessions-section — keeps the automations
// engine's state (view filter, completed/bin position) alive across tab switches.
function _setAutomationsContext(container) {
  const section = document.getElementById('automations-section');
  const page = document.getElementById('tab-agents');
  if (!section) return;
  const target = container || page;
  if (target && section.parentElement !== target) target.appendChild(section);
}

function _renderAllAgentsCard(container) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <button type="button" class="agent-card-tabs-chev left" aria-label="Scroll tabs left" tabindex="-1">&#10094;</button>
      <div class="agent-card-tabs" role="tablist"></div>
      <button type="button" class="agent-card-tabs-chev right" aria-label="Scroll tabs right" tabindex="-1">&#10095;</button>
    </div>`;

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel';
  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  const body = document.createElement('div');
  body.className = 'agent-detail-body agent-detail-body-all-agents';
  content.appendChild(body);
  panel.appendChild(content);

  const renderTab = () => {
    _destroyAllAgentsSessionsShell();
    setSessionsAgentContext(null);
    _setAutomationsContext(null);
    stopAutomations();
    _destroyAllAgentsModelTable();
    body.innerHTML = '';
    _renderAllAgentsTabBody(body, _allAgentsTab);
    // Rebuild the strip AFTER the body. The bar is DERIVED from the active page on
    // every render, which is exactly what drops the EPHEMERAL tab for a page the
    // user has just navigated away from. Running after the body matters because
    // _renderAllAgentsTabBody can fall back to Config for an unknown page.
    _populateAllAgentsTabBar(tabBar, renderTab);
  };

  const tabBar = card.querySelector('.agent-card-tabs');
  _populateAllAgentsTabBar(tabBar, renderTab);
  _wireTabCarousel(card.querySelector('.agent-card-tabs-wrap'));

  const row = document.createElement('div');
  row.className = 'agent-row expanded agent-row-all-agents';
  row.appendChild(card);
  row.appendChild(panel);
  container.appendChild(row);
  renderTab();
}

// ── All Agents tab body (shared by the cards card and the list view) ───────────

function _renderAllAgentsTabBody(body, tab) {
  // The route-level Ask or Search launcher owns the visible input. Clear a prior
  // tab's controller before mounting the current surface.
  delete body._agentsSearchController;
  delete body.dataset.agentsSearchController;
  body.classList.remove('all-agents-sessions-body', 'all-agents-automations-body');
  // Only an UNKNOWN page falls back to Config. A page that is merely not a favorite
  // still renders — the header shows it as an ephemeral tab (see
  // _populateAllAgentsTabBar) — so an unstarred open page is never left invisible.
  if (!_myAgentsPageLabels().has(tab)) { tab = 'models'; _allAgentsTab = 'models'; }
  if (tab === 'config') {
    _renderAllAgentsConfigTab(body);
    return;
  }
  if (tab === 'sessions') {
    body.classList.add('all-agents-sessions-body');
    setSessionsAgentContext(null, body);
    _mountAllAgentsSessionsShell(body);
    return;
  }
  if (tab === 'automations') {
    body.classList.add('all-agents-automations-body');
    _setAutomationsContext(body);
    startAutomations();
    return;
  }
  if (tab === 'models') {
    // MODEL-ANALYTICS BOUNDARY: a standalone sibling directly before the
    if (!mountUserModelCatalog || !initModelAnalytics) {
      body.textContent = 'Loading model catalog…';
      Promise.all([import('../../app/settings/agent-settings/agent-settings.js'), import('../../app/settings/agent-settings/model-analytics.js?v=7')])
        .then(([catalog, analytics]) => {
          mountUserModelCatalog = catalog.mountUserModelCatalog;
          initModelAnalytics = analytics.initModelAnalytics;
          if (_allAgentsTab === 'models' && body.isConnected) _renderList();
        })
        .catch((error) => { console.error('agents: model catalog failed', error); if (body.isConnected) body.textContent = 'Unable to load model catalog.'; });
      return;
    // available-models selection table; never a table row or descendant.
    }
    const analyticsHost = document.createElement('div');
    analyticsHost.className = 'all-agents-model-analytics-host';
    body.appendChild(analyticsHost);
    _allAgentsModelAnalytics = initModelAnalytics(analyticsHost, {
      fetchFn: (url, options = {}) => fetch(url, {
        ...options,
        headers: { ...authHeaders(), ...(options.headers || {}) },
      }),
      apiPath,
      scope: 'user',
      nestedAgentStacks: true,
      description: 'Compare each active agent side by side, with its model usage stacked within the bar.',
    });
    const tableHost = document.createElement('div');
    tableHost.className = 'all-agents-model-table-host';
    body.appendChild(tableHost);
    _allAgentsModelTable = mountUserModelCatalog(tableHost, {
      onModelChange: () => {
        app.refreshModelContext?.();
        _allAgentsModelAnalytics?.reload();
      },
      // MODEL-LEGEND-ALIAS: the catalog roster owns the display names, so hand them
      // to the chart above it; its legend labels each model the way the rows do.
      onRoster: (providers) => _allAgentsModelAnalytics?.setModelLabels(providers),
      // MODEL-ROW-FOCUS: opening a catalog row narrows the chart above to that model;
      // closing the row (or reloading the roster) hands the whole group back.
      onModelFocus: (model) => _allAgentsModelAnalytics?.setModelFocus(model),
    });
    return;
  }
  import('../../../shared/js/admin-ability-table.js').then(({ build }) => build(body, {
    abilityStates: {},
    canEdit: _userIsAdmin,
    showSearch: false,
    onSearchController: (runFilter) => {
      body._agentsSearchController = runFilter;
      body.dataset.agentsSearchController = 'abilities';
    },
  })).catch((error) => {
    console.error('agents: all-agents abilities tab failed', error);
    body.textContent = 'Unable to load abilities.';
  });
}

// ── My Agents header tabs (the All Agents card) ────────────────────────────────

// Reorder every rendered My Agents header strip in place. Scoped to the All Agents
// card: per-agent strips carry a different id set and keep their own helper
// (see _syncTabBarOrder).
function _syncAllAgentsTabOrder(ids) {
  document.querySelectorAll('.agent-row-all-agents .agent-card-tabs, .agents-list-tab-carousel[data-view="all-agents"] .agent-card-tabs').forEach((bar) => {
    const buttons = new Map();
    bar.querySelectorAll(':scope > .agents-detail-tab').forEach((btn) => buttons.set(btn.dataset.tab, btn));
    ids.forEach((id) => {
      const btn = buttons.get(id);
      if (btn) bar.appendChild(btn);   // appendChild MOVES an existing node, in order
    });
    // Keep the ephemeral (not-favorited) tab pinned LAST — it is absent from `ids`,
    // so the reorder above would otherwise shuffle it to the front.
    const ephemeral = bar.querySelector(':scope > .agents-detail-tab.ephemeral');
    if (ephemeral) bar.appendChild(ephemeral);
  });
}

// Build the My Agents header strip from the favorites-driven tab list. Mirrors
// _populateAgentTabBar (same `.agents-detail-tab` contract, same drag-to-reorder
// convention), but it writes the My Agents preference record.
function _populateAllAgentsTabBar(tabBar, onChange = null) {
  tabBar.innerHTML = '';
  const labels = _myAgentsPageLabels();
  const rows = _allAgentsTabs();
  const favoriteIds = new Set(rows.map(([id]) => id));
  // Same ephemeral rule as the per-agent strip: an open page that is NOT a favorite
  // keeps a tab pinned to the very END, recomputed on every render and never
  // persisted, so it disappears the moment the user navigates away from that page.
  const ephemeralId = (_allAgentsTab && !favoriteIds.has(_allAgentsTab) && labels.has(_allAgentsTab))
    ? _allAgentsTab
    : null;
  if (ephemeralId) rows.push([ephemeralId, labels.get(ephemeralId)]);

  for (const [key, label] of rows) {
    const isEphemeral = key === ephemeralId;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agents-detail-tab'
      + (isEphemeral ? ' ephemeral' : '')
      + (_allAgentsTab === key ? ' active' : '');
    btn.dataset.tab = key;
    btn.textContent = label;
    if (isEphemeral) btn.title = 'Temporary tab — this page is not a favorite';
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      if (_allAgentsTab === key) return;
      _allAgentsTab = key;
      // Remember the My Agents card's last sub-page across reloads.
      _saveViewState();
      onChange?.();
    });

    // Drag-to-reorder. The header order IS this user's global My Agents favorite
    // order, so a drop rewrites the shared preference and its server record.
    // Ephemeral tabs live outside that order, so they are not draggable.
    btn.draggable = !isEphemeral;
    btn.addEventListener('dragstart', (e) => {
      _draggingTabId = key;
      btn.classList.add('dragging');
      try {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', key);
      } catch (_) { /* dataTransfer restricted — the module var still carries it */ }
    });
    btn.addEventListener('dragend', () => {
      _draggingTabId = null;
      btn.classList.remove('dragging');
      tabBar.querySelectorAll('.agents-detail-tab').forEach((el) => el.classList.remove('drag-over'));
    });
    btn.addEventListener('dragover', (e) => {
      if (!_draggingTabId || _draggingTabId === key) return;
      // preventDefault on EVERY dragover, or the drop event never fires.
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      tabBar.querySelectorAll('.agents-detail-tab').forEach((el) => el.classList.remove('drag-over'));
      btn.classList.add('drag-over');
    });
    btn.addEventListener('dragleave', () => btn.classList.remove('drag-over'));
    btn.addEventListener('drop', (e) => {
      e.preventDefault();
      btn.classList.remove('drag-over');
      const fromId = _draggingTabId;
      _draggingTabId = null;
      const toId = key;
      if (!fromId || fromId === toId) return;      // self-drop is a no-op
      const ids = readMyAgentsPageFavorites();
      const fromIdx = ids.indexOf(fromId);
      if (fromIdx < 0) return;                     // unknown id — leave order alone
      ids.splice(fromIdx, 1);
      const toIdx = ids.indexOf(toId);
      if (toIdx < 0) ids.push(fromId); else ids.splice(toIdx, 0, fromId);
      const next = writeMyAgentsPageFavorites(ids);
      _syncAllAgentsTabOrder(next);
    });

    tabBar.appendChild(btn);
  }
}

// The My Agents Config tab: the same JSON-driven directory the per-agent Config
// page renders, wired to this card's own favorites record.
async function _renderAllAgentsConfigTab(body) {
  const mod = await import('../my-pages/config/page.js');
  if (_allAgentsTab !== 'config') return;
  mod.renderMyAgentsConfigDirectory(body, {
    favoriteIds: readMyAgentsPageFavorites(),
    onNavigate: (pageId) => {
      if (!(myPagesCatalog.pages || []).some((page) => page.id === pageId)) return;
      _allAgentsTab = pageId;
      _renderList();
      _saveViewState();
    },
    onToggleFavorite: (pageId) => {
      const favorites = toggleMyAgentsPageFavorite(pageId);
      // A page unstarred while active should never become an invisible state.
      if (_allAgentsTab === pageId && !favorites.includes(pageId)) _allAgentsTab = 'models';
      // Rebuild only the existing My Agents header strip. The Config landing, its
      // scroll position and its rows remain mounted — no remount on a star click.
      // Scoped to .agent-row-all-agents so the per-agent carousels are untouched.
      document.querySelectorAll('.agent-row-all-agents .agent-card-tabs, .agents-list-tab-carousel[data-view="all-agents"] .agent-card-tabs').forEach((tabBar) => {
        _populateAllAgentsTabBar(tabBar, () => _renderList());
      });
      _saveViewState();
      return favorites;
    },
  });
}

// ── List view ──────────────────────────────────────────────────────────────────
// The "List" view type: each agent/card becomes a row in a vertical list, every
// row carries a ⋯ more button (Start a chat / Delete), and below the list a
// sub-pages dropdown picks which sub-page content renders in the detail region.

function _agentTabs(agent) {
  // Canonical tab keys ONLY. 'agent-loop'/'abilities' are the keys the detail
  // panel dispatches on (_renderTestTab / _renderConnectionsTab) and the keys the
  // header carousel writes. The old 'test'/'connections' aliases had no branch in
  // that dispatch, so picking them here rendered an EMPTY detail body while the
  // route said otherwise — and they poisoned the per-agent tab memory, so the row
  // menu and the carousel could not agree on the same sub-page.
  const tabs = [['sessions', 'Sessions'], ['config', 'Config'], ['prompts', 'Prompts'], ['agent-loop', 'Agent Loop'], ['abilities', 'Abilities']];
  if (_userIsAdmin) tabs.push(['members', 'Members']);
  tabs.push(['monetization', 'Monetization']);
  return tabs;
}

// Every My Agents page id -> its header label, straight from the my-pages catalog.
// This is also the validity set for ephemeral tabs (an unknown id never renders).
function _myAgentsPageLabels() {
  return new Map((myPagesCatalog.pages || []).map((page) => [page.id, page.tabLabel || page.title]));
}

function _allAgentsTabs() {
  // ONE definition of the My Agents tabs, driven by the my-pages catalog and the
  // shared favorites helper, so the card strip and the row menu cannot drift.
  // Favorites decide both the visible tabs and their user-chosen order; Config is
  // required by the preference helper, but it is not pinned.
  const labels = _myAgentsPageLabels();
  return readMyAgentsPageFavorites()
    .map((pageId) => [pageId, labels.get(pageId)])
    .filter(([, label]) => Boolean(label));
}

function _renderListView() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  // Move the shared movable section out before rebuilding (same as _renderList).
  _destroyAllAgentsSessionsShell();
  setSessionsAgentContext(null);
  _setAutomationsContext(null);
  stopAutomations();
  _destroyAllAgentsModelTable();

  const activeId = _activeId();
  const allAgentsActive = !activeId && !_binView && !_clonesView;
  grid.classList.remove('agents-skeleton');
  grid.classList.remove('carousel');
  grid.classList.add('agents-list-view');
  grid.innerHTML = '';

  const topRow = document.createElement('div');
  topRow.className = 'agents-top-row agents-list-top';
  const list = document.createElement('div');
  list.className = 'agents-list';
  list.setAttribute('role', 'list');
  topRow.appendChild(list);
  grid.appendChild(topRow);

  // Mobile list mode is a personal compact sidebar: the selector below always
  // exposes the complete roster, while only personal favorites remain visible.
  const ordered = sortAgentsForDisplay(_agents, app.currentUserId);
  const favoriteIds = readMobileAgentFavorites();
  const selector = _buildMyAgentsSelector(ordered, favoriteIds, allAgentsActive);
  list.appendChild(selector);
  // The roster popover belongs to the LIST, not to the selector row. Anchored to
  // that row it was drawn straight over the favorite rows beneath it (and it stays
  // open across star toggles by design), so a tap on a row's icon or name landed on
  // the open roster instead of the row and switching agents silently did nothing.
  // Hanging it off .agents-list-top puts it UNDER the list, so rows stay live.
  const rosterMenu = selector.querySelector('.agents-my-agents-menu');
  if (rosterMenu) topRow.appendChild(rosterMenu);

  const favorites = ordered.filter((agent) => favoriteIds.includes(agent.id));
  if (!favorites.length) {
    const empty = document.createElement('div');
    empty.className = 'agents-list-empty';
    empty.textContent = 'Choose an agent from My Agents, then star it to keep it here.';
    list.appendChild(empty);
  }
  for (const agent of favorites) {
    list.appendChild(_buildAgentListRow({
      agent,
      name: _displayName(agent),
      active: activeId === agent.id,
    }));
  }

  // Horizontal divider separating the agent rows from the sub-page content area.
  const divider = document.createElement('div');
  divider.className = 'agents-list-divider';
  grid.appendChild(divider);
  // Sub-page content region. Row More (⋮) menus remain alternate navigation,
  // while an active agent also receives the shared desktop tab carousel above its
  // content — see _buildListAgentTabCarousel.
  _bindListRowMenuCloseHandlers();
  const region = document.createElement('div');
  region.id = 'agents-detail-region';
  region.classList.add('open');
  grid.appendChild(region);
  // When the "+" create button is active the active id is the mock/new-agent id,
  // so render that create card (mirrors _renderClonesGrid) instead of the list.
  const mockAgent = _createMockAgent();
  let activeAgent = (!_binView && !_clonesView && activeId === MOCK_AGENT_ID) ? mockAgent : (_agents.find(a => a.id === activeId) || null);
  _renderListSubPage(region, allAgentsActive ? null : activeAgent);

  _updateBinToolbar();
  _syncMergedLayout();
}

function _buildMyAgentsSelector(ordered, favoriteIds, active) {
  // Preserve the original compact "All Agents" row structure. The row BODY opens
  // the My Agents surface — exactly like every agent row opens its agent — on the
  // My Agents sub-page this account last had open; the chevron remains a pure
  // roster toggle and the row's own +/⋮ buttons keep their own behavior.
  const selector = document.createElement('div');
  selector.className = 'agents-list-row agents-my-agents-selector' + (active ? ' active' : '');
  selector.setAttribute('role', 'listitem');
  selector.dataset.view = 'all-agents';

  const avatar = document.createElement('div');
  avatar.className = 'agents-list-avatar color-purple';
  avatar.innerHTML = icon('layout-grid', { size: '20px' });

  const label = document.createElement('div');
  label.className = 'agents-list-label';
  const name = document.createElement('span');
  name.className = 'agents-list-name';
  name.textContent = 'My Agents';

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'agents-my-agents-trigger';
  trigger.setAttribute('aria-label', 'Choose from all my agents');
  trigger.setAttribute('aria-haspopup', 'menu');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.innerHTML = icon('chevron-down', { size: '16px' });
  label.append(name, trigger);

  const actions = document.createElement('div');
  actions.className = 'agents-list-row-actions-cluster';
  actions.appendChild(_buildListRowCreateAgentButton());
  actions.appendChild(_buildListRowMoreButton({ menu: _buildAllAgentsRowMenu(), ariaLabel: 'More options for My Agents' }));

  const menu = document.createElement('div');
  menu.className = 'agents-my-agents-menu';
  menu.setAttribute('role', 'menu');
  for (const agent of ordered) {
    const item = document.createElement('div');
    item.className = 'agents-my-agents-menu-row';
    item.setAttribute('role', 'none');

    const select = document.createElement('button');
    select.type = 'button';
    select.className = 'agents-my-agents-menu-select';
    select.setAttribute('role', 'menuitem');
    select.setAttribute('aria-label', `Open ${_displayName(agent)}`);
    select.innerHTML = `<span class="agents-list-avatar agents-my-agents-menu-avatar ${_iconColor(agent)}">${_renderAgentIcon(agent, '14px')}</span><span class="agents-my-agents-menu-name">${_esc(_displayName(agent))}</span>`;
    select.addEventListener('click', () => {
      closeListRowMenus();
      _activateAgentFromUserClick(agent);
    });
    const actions = document.createElement('div');
    actions.className = 'agents-my-agents-menu-actions';
    actions.appendChild(_buildMobileFavoriteButton(agent, favoriteIds, { keepMenuOpen: true }));
    actions.appendChild(_buildListRowChatButton(agent));
    actions.appendChild(_buildListRowMoreButton({
      menu: _buildListRowMenu(agent),
      ariaLabel: `More options for ${_displayName(agent)}`,
      preserveMyAgentsMenu: true,
    }));
    item.append(select, actions);
    menu.appendChild(item);
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    const wasOpen = menu.classList.contains('open');
    closeListRowMenus();
    if (!wasOpen) menu.classList.add('open');
    trigger.setAttribute('aria-expanded', menu.classList.contains('open') ? 'true' : 'false');
  });
  // Row body → My Agents. Mirrors the agent rows: re-render the All Agents
  // surface, which restores that surface's own last sub-page (_allAgentsTab, a
  // persisted view-state value; _renderAllAgentsTabBody falls back to Config when
  // that page is no longer a favorite). The chevron, the roster and the row's own
  // +/⋮ buttons are excluded so they keep their behavior.
  selector.addEventListener('click', (e) => {
    if (e.target.closest('.agents-my-agents-trigger, .agents-my-agents-menu, .agents-list-row-actions-cluster')) return;
    closeListRowMenus();
    if (selector.classList.contains('active')) return;   // My Agents already open
    // The same hand-off the agent rows perform, so the previous agent's sessions
    // table and automations engine are released before the rebuild.
    _clearExpanded();
    setSessionsAgentContext(null);
    _setAutomationsContext(null);
    stopAutomations();
    replaceRouteParams({ agent: null, agentView: null });
    _renderList();
    _saveViewState();
  });
  selector.append(avatar, label, actions, menu);
  return selector;
}

function _buildMobileFavoriteButton(agent, favoriteIds, { keepMenuOpen = false } = {}) {
  const isFavorite = favoriteIds.includes(agent.id);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'agents-list-favorite-btn' + (isFavorite ? ' is-favorite' : '');
  btn.title = isFavorite ? 'Remove from sidebar' : 'Add to sidebar';
  btn.setAttribute('aria-label', `${isFavorite ? 'Remove' : 'Add'} ${_displayName(agent)} ${isFavorite ? 'from' : 'to'} sidebar`);
  btn.setAttribute('aria-pressed', isFavorite ? 'true' : 'false');
  btn.innerHTML = icon('star', { size: '16px' });
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    toggleMobileAgentFavorite(agent.id);
    _renderList();
    if (keepMenuOpen) {
      // The list rerender refreshes every star state. Restore only the outer
      // roster so consecutive favorite edits do not require reopening it.
      const roster = document.querySelector('.agents-my-agents-menu');
      const trigger = document.querySelector('.agents-my-agents-trigger');
      roster?.classList.add('open');
      trigger?.setAttribute('aria-expanded', 'true');
    }
  });
  return btn;
}

function _buildAgentListRow({ allAgents, agent, name, iconHtml, active }) {
  const row = document.createElement('div');
  row.className = 'agents-list-row' + (active ? ' active' : '');
  row.setAttribute('role', 'listitem');
  if (allAgents) row.dataset.view = 'all-agents';
  else if (agent) row.dataset.agentId = agent.id;

  const avatar = document.createElement('div');
  avatar.className = 'agents-list-avatar';
  const label = document.createElement('div');
  label.className = 'agents-list-label';
  const nameEl = document.createElement('span');
  nameEl.className = 'agents-list-name';
  nameEl.textContent = name;
  label.appendChild(nameEl);

  if (allAgents) {
    avatar.classList.add('color-purple');
    avatar.innerHTML = iconHtml;
  } else if (agent) {
    avatar.classList.add(_iconColor(agent));
    avatar.innerHTML = _renderAgentIcon(agent, '18px');
    const dot = document.createElement('span');
    dot.className = 'agent-status-dot' + (_isAgentRunning(agent.id) ? ' running' : '');
    label.appendChild(dot);
  }

  row.appendChild(avatar);
  row.appendChild(label);

  row.addEventListener('click', () => {
    if (allAgents) {
      _clearExpanded();
      setSessionsAgentContext(null);
      _setAutomationsContext(null);
      stopAutomations();
      _renderList();
      _saveViewState();
    } else if (agent) {
      _activateAgentFromUserClick(agent);
    }
  });

  if (allAgents) {
    const cluster = document.createElement('div');
    cluster.className = 'agents-list-row-actions-cluster';
    cluster.appendChild(_buildListRowCreateAgentButton());
    cluster.appendChild(_buildListRowMoreButton({ menu: _buildAllAgentsRowMenu(), ariaLabel: 'More options for All Agents' }));
    row.appendChild(cluster);
  } else if (agent && !_isMockAgent(agent)) {
    const cluster = document.createElement('div');
    cluster.className = 'agents-list-row-actions-cluster';
    cluster.appendChild(_buildListRowChatButton(agent));
    cluster.appendChild(_buildListRowMoreButton({ menu: _buildListRowMenu(agent), ariaLabel: `More options for ${_displayName(agent)}` }));
    row.appendChild(cluster);
  }
  return row;
}

function _buildListRowMoreButton({ menu, ariaLabel, preserveMyAgentsMenu = false }) {
  const wrap = document.createElement('div');
  wrap.className = 'agents-list-row-actions';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'agents-list-more-btn';
  btn.title = 'More options';
  btn.setAttribute('aria-label', ariaLabel);
  btn.setAttribute('aria-haspopup', 'true');
  btn.setAttribute('aria-expanded', 'false');
  btn.innerHTML = icon('more-vertical', { size: '16px' });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const wasOpen = menu.classList.contains('open');
    closeListRowMenus({ preserveMyAgentsMenu });
    if (!wasOpen) menu.classList.add('open');  // proper toggle: closing a menu that's already open
    btn.setAttribute('aria-expanded', menu.classList.contains('open') ? 'true' : 'false');
  });
  wrap.appendChild(btn);
  wrap.appendChild(menu);
  return wrap;
}

function _buildListRowChatButton(agent) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'agents-list-chat-btn';
  btn.title = 'Start a new session';
  btn.setAttribute('aria-label', `Start a new session with ${_displayName(agent)}`);
  btn.innerHTML = icon('message-square-plus', { size: '16px' });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    closeListRowMenus();
    _startChatWithAgent(agent);
  });
  return btn;
}

function _buildListRowCreateAgentButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'agents-list-create-agent-btn';
  btn.title = 'Create a new agent';
  btn.setAttribute('aria-label', 'Create a new agent');
  btn.innerHTML = icon('plus', { size: '16px' });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    closeListRowMenus();
    _selectAgent(_createMockAgent());
  });
  return btn;
}

function closeListRowMenus({ preserveMyAgentsMenu = false } = {}) {
  const menuSelector = preserveMyAgentsMenu
    ? '.agents-list-row-menu.open'
    : '.agents-list-row-menu.open, .agents-my-agents-menu.open';
  document.querySelectorAll(menuSelector).forEach((m) => m.classList.remove('open'));
  const buttonSelector = preserveMyAgentsMenu
    ? '.agents-list-more-btn[aria-expanded="true"]'
    : '.agents-list-more-btn[aria-expanded="true"], .agents-my-agents-trigger[aria-expanded="true"]';
  document.querySelectorAll(buttonSelector).forEach((b) => b.setAttribute('aria-expanded', 'false'));
}

// A sub-page pick in a row's More (⋮) menu must OPEN that sub-page in the same
// click — the selection is a navigation, not a stored preference. Two halves:
//   1. make the row the active context (open its agent, or return to All Agents),
//      otherwise the region below keeps rendering whatever was already open and
//      the pick looks "queued" until the row's heading is clicked;
//   2. then apply the chosen tab.
// Both menus (All Agents and per-agent) share this one path so the two cannot
// drift (PREVENTIVE-COMMENTS). `agent === null` means the All Agents row.
function _openSubPageFromMenu(agent, key) {
  if (agent) {
    if (_expandedAgents.get(agent.id)?.tab === key) return;  // already showing it
    // Same session-table hand-off the row's heading click performs, so the rows
    // scoped to the previously open agent never repaint before the new ones do.
    if (!_expandedAgents.has(agent.id)) {
      setSessionsAgentContext(agent.id, null, { agentName: agent.name || 'Agent' });
    }
    _setActive(agent.id, key);
    replaceRouteParams({ agent: agent.id, agentView: key === 'model' ? null : key });
  } else {
    if (!_activeId() && _allAgentsTab === key) return;       // already showing it
    _clearExpanded();
    _allAgentsTab = key;
  }
  _renderList();
  _saveViewState();
}

function _buildListRowMenu(agent) {
  const menu = document.createElement('div');
  menu.className = 'agents-list-row-menu';
  // Sub-pages group — picking one renders that sub-page in the detail region below
  // the list (replaces the old standalone sub-pages dropdown).
  const current = _canonicalTabKey(_expandedAgents.get(agent.id)?.tab) || 'model';
  menu.appendChild(_buildSubpageMenuItems(_agentTabs(agent), current, (key) => {
    _openSubPageFromMenu(agent, key);
  }));
  // Only persisted custom agents can be trashed; system/template rows can't.
  if (agent.source === 'custom') {
    menu.appendChild(_buildMenuDivider());
    menu.appendChild(_buildMenuAction('Delete', 'trash-2', () => { _deleteAgentFromList(agent); }, true));
  }
  return menu;
}

function _buildAllAgentsRowMenu() {
  const menu = document.createElement('div');
  menu.className = 'agents-list-row-menu';
  menu.appendChild(_buildSubpageMenuItems(_allAgentsTabs(), _allAgentsTab, (key) => {
    _openSubPageFromMenu(null, key);
  }));
  return menu;
}

function _buildSubpageMenuItems(tabs, current, onSelect) {
  const frag = document.createDocumentFragment();
  tabs.forEach(([key, tabLabel]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'agents-list-row-menu-item subpage' + (key === current ? ' active' : '');
    item.setAttribute('role', 'menuitem');
    item.dataset.value = key;
    item.innerHTML = icon(_subpageIcon(key), { size: '14px' }) + `<span>${_esc(tabLabel)}</span>`;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      closeListRowMenus();
      onSelect(key);
    });
    frag.appendChild(item);
  });
  return frag;
}

function _buildMenuDivider() {
  const d = document.createElement('div');
  d.className = 'agents-list-row-menu-divider';
  return d;
}

function _buildMenuAction(label, iconName, onClick, danger) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'agents-list-row-menu-item' + (danger ? ' danger' : '');
  btn.innerHTML = icon(iconName, { size: '14px' }) + `<span>${_esc(label)}</span>`;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    closeListRowMenus();
    onClick();
  });
  return btn;
}

// Soft-delete an agent (and, server-side, all its sessions) to the recycling bin.
// Mirrors _trashAgent (tab-config.js); the confirmation is the dedicated two-step
// trash button there, but a list-row Delete is a direct, deliberate action so we
// soft-delete immediately and surface the bin notice for undo.
async function _deleteAgentFromList(agent) {
  const name = agent.name || agent.id;
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE', headers: { ...authHeaders() } });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      window.__agentsNotice?.(`Agent "${name}" could NOT be moved to the recycling bin — ${err.detail || `the server returned an error (HTTP ${res.status})`}`, { kind: 'error' });
      return;
    }
  } catch (e) {
    window.__agentsNotice?.(`Agent "${name}" could NOT be moved to the recycling bin — network error: ${e.message}`, { kind: 'error' });
    return;
  }
  try { if (typeof app.populateAgentSelect === 'function') await app.populateAgentSelect(app.currentUserId); } catch (_) { /* non-fatal */ }
  _clearExpanded();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
  window.__agentsNotice?.(`Agent "${name}" moved to the recycling bin — its sessions went with it.`, {
    action: { label: 'Open bin', fn: () => { if (typeof window.__agentsOpenBin === 'function') window.__agentsOpenBin(); } },
  });
}

// Sub-page tab → lucide icon (kept consistent with the per-tab icon used elsewhere).
const _SUBPAGE_ICONS = {
  sessions: 'message-square',
  config: 'settings',
  prompts: 'file-text',
  test: 'refresh-cw',       // "Agent Loop"
  connections: 'plug',       // "Abilities" (single agent)
  members: 'users',
  monetization: 'credit-card',
  automations: 'zap',
  models: 'cpu',
  abilities: 'sparkles',     // "Abilities" (All Agents)
};
function _subpageIcon(key) {
  return _SUBPAGE_ICONS[key] || 'chevron-right';
}

// List-row More (⋮) menu close handlers: clicking outside a row's action wrap or
// pressing Escape closes any open row menu. (This replaces the old separate
// sub-pages dropdown, which had the same behaviour.)
let _listRowMenuCloseBound = false;
function _bindListRowMenuCloseHandlers() {
  if (_listRowMenuCloseBound) return;
  _listRowMenuCloseBound = true;
  document.addEventListener('click', (e) => {
    // A click inside an open menu or on its ⋮ toggle is handled by that element's
    // own listener; any other click outside the more panel closes it. Guard the
    // target so a programmatic dispatch on a non-Element (e.g. document) can't throw.
    const t = e.target;
    if (t && typeof t.closest === 'function') {
      if (t.closest('.agents-list-row-menu, .agents-my-agents-menu')) return;
      if (t.closest('.agents-list-more-btn, .agents-my-agents-trigger')) return;
    }
    closeListRowMenus();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeListRowMenus();
  });
}

function _buildListAgentTabCarousel(agent, panel) {
  // Reuse the desktop card carousel verbatim: same page-favorite ordering,
  // permissions, active state, async badges, route update, and native-scroll
  // wiring. Mobile differs only in placement above the detail panel.
  const wrap = document.createElement('div');
  wrap.className = 'agent-card-tabs-wrap agents-list-tab-carousel';
  wrap.innerHTML = `
    <button type="button" class="agent-card-tabs-chev left" aria-label="Scroll tabs left" tabindex="-1">&#10094;</button>
    <div class="agent-card-tabs" role="tablist"></div>
    <button type="button" class="agent-card-tabs-chev right" aria-label="Scroll tabs right" tabindex="-1">&#10095;</button>`;
  const tabBar = wrap.querySelector('.agent-card-tabs');
  if (tabBar) _populateAgentTabBar(tabBar, agent, panel);
  _wireTabCarousel(wrap);
  return wrap;
}

function _buildListAllAgentsTabCarousel() {
  // Mobile parity for the My Agents card. _renderAllAgentsCard supplies the tab
  // strip in the desktop/cards view, but the merged (<=800px) layout has no
  // .agent-row-all-agents at all, so its sub-page favorites were missing entirely.
  // Same carousel contract as the agent tabs — including the EPHEMERAL tab for an
  // open page that is not a favorite.
  const wrap = document.createElement('div');
  wrap.className = 'agent-card-tabs-wrap agents-list-tab-carousel';
  // Marks this as the All Agents strip so _syncAllAgentsTabOrder (drag reorder) and
  // the star-toggle rebuild can find it outside .agent-row-all-agents.
  wrap.dataset.view = 'all-agents';
  wrap.innerHTML = `
    <button type="button" class="agent-card-tabs-chev left" aria-label="Scroll tabs left" tabindex="-1">&#10094;</button>
    <div class="agent-card-tabs" role="tablist"></div>
    <button type="button" class="agent-card-tabs-chev right" aria-label="Scroll tabs right" tabindex="-1">&#10095;</button>`;
  const tabBar = wrap.querySelector('.agent-card-tabs');
  if (tabBar) _populateAllAgentsTabBar(tabBar, () => _renderList());
  _wireTabCarousel(wrap);
  return wrap;
}

function _renderListSubPage(region, agent) {
  region.innerHTML = '';
  if (!agent) {
    _destroyAllAgentsSessionsShell();
    setSessionsAgentContext(null);
    _setAutomationsContext(null);
    stopAutomations();
    _destroyAllAgentsModelTable();
    // Mobile parity: the individual agents already show their favorite sub-page
    // tabs here, so the My Agents card must show its own on the same contract.
    region.appendChild(_buildListAllAgentsTabCarousel());
    const body = document.createElement('div');
    body.className = 'agent-detail-body agent-detail-body-all-agents';
    region.appendChild(body);
    _renderAllAgentsTabBody(body, _allAgentsTab);
    // In list view there is no carousel/.agent-row-all-agents for the sessions
    // shell to measure, so it never opts into the docked footer. Pin the page-level
    // footer dock the same way the cards view does, so the Sessions "footer" stays
    // visible at the bottom of the sub-page. It is reset by the _destroy call above
    // on every (re)render and by _destroyAllAgentsSessionsShell on teardown.
    if (_allAgentsTab === 'sessions') _setAllAgentsSessionsFooterDock(true);
    return;
  }
  const panel = _buildDetailPanel(agent);
  region.appendChild(_buildListAgentTabCarousel(agent, panel));
  region.appendChild(panel);
}

function _renderSquare(container, agent, isActive) {
  const isMock = _isMockAgent(agent);
  const colorClass = isMock ? 'color-blue' : _iconColor(agent);
  const iconHtml = isMock ? icon('plus', { size: '36px' }) : _renderAgentIcon(agent, '24px');
  const name = isMock ? 'New Agent' : _displayName(agent);

  const card = document.createElement('div');
  card.className = 'agent-card agent-square' + (isActive ? ' activated' : '');
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap ${colorClass}">${iconHtml}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">${_esc(name)}</span>
          ${isMock ? '' : `<span class="agent-status-dot${_isAgentRunning(agent.id) ? ' running' : ''}"></span>`}
        </div>
      </div>
    </div>`;
  card.addEventListener('click', () => _activateAgentFromUserClick(agent));

  if (!isMock) {
    const sqIconWrap = card.querySelector('.agent-card-icon-wrap');
    if (sqIconWrap) {
      let _lpTimer = null;
      sqIconWrap.addEventListener('pointerdown', e => {
        _lpTimer = setTimeout(() => {
          _lpTimer = null;
          e.stopPropagation();
          _openIconPicker(sqIconWrap, agent, null);
        }, 500);
      });
      sqIconWrap.addEventListener('pointerup', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
      sqIconWrap.addEventListener('pointerleave', () => { if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; } });
    }
  }

  const row = document.createElement('div');
  row.className = 'agent-row';
  row.dataset.agentId = agent.id;

  if (!isMock && agent.source === 'custom') {
    const on = _selectedIds.has(agent.id);
    if (on) row.classList.add('selected');
    const box = document.createElement('div');
    box.className = 'agent-select-box';
    box.title = 'Select';
    box.innerHTML = on ? icon('check', { size: '13px' }) : '';
    box.addEventListener('click', e => {
      e.stopPropagation();
      _toggleSelect(agent.id);
    });
    card.appendChild(box);
  }

  // Chat-message button (bottom-right) — starts a new chat with this agent.
  // Skipped for the mock "New Agent" tile and in the recycling bin.
  if (!isMock && !_binView) {
    const chatBtn = document.createElement('button');
    chatBtn.type = 'button';
    chatBtn.className = 'agent-square-chat-btn';
    chatBtn.title = 'New chat with this agent';
    chatBtn.setAttribute('aria-label', 'New chat with this agent');
    chatBtn.innerHTML = icon('message-square', { size: '13px' });
    chatBtn.addEventListener('click', e => {
      e.stopPropagation();
      _startChatWithAgent(agent);
    });
    card.appendChild(chatBtn);
  }

  row.appendChild(card);
  container.appendChild(row);
}

function _renderClonesTile(container) {
  const card = document.createElement('div');
  card.className = 'agent-card agent-square';
  card.innerHTML = `
    <div class="agent-card-top">
      <div class="agent-card-icon-wrap color-teal">${icon('copy', { size: '24px' })}</div>
      <div class="agent-card-meta">
        <div class="agent-card-name-row">
          <span class="agent-card-name">Clones</span>
        </div>
      </div>
    </div>`;
  card.addEventListener('click', () => _enterClones());
  const row = document.createElement('div');
  row.className = 'agent-row';
  row.appendChild(card);
  container.appendChild(row);
}

function _renderClonesList(container) {
  const groups = {};
  for (const agent of _agents) {
    let orchId = agent.orchestrator_agent_id;
    if (!orchId) {
      try {
        const meta = typeof agent.metadata === 'string' ? JSON.parse(agent.metadata) : (agent.metadata || {});
        orchId = meta.clone_of || null;
      } catch (_) { orchId = null; }
    }
    if (!groups[orchId]) groups[orchId] = { orchId, agents: [] };
    groups[orchId].agents.push(agent);
  }

  for (const [orchId, group] of Object.entries(groups)) {
    let orchName = null;
    const first = group.agents[0];
    if (first.orchestrator_name) {
      orchName = first.orchestrator_name;
    } else {
      try {
        const meta = typeof first.metadata === 'string' ? JSON.parse(first.metadata) : (first.metadata || {});
        orchName = meta.clone_of_name || null;
      } catch (_) {}
    }
    const heading = document.createElement('div');
    heading.className = 'agents-clones-group-heading';
    heading.textContent = orchName ? `From: ${orchName}` : 'Unknown orchestrator';
    container.appendChild(heading);

    for (const agent of group.agents) {
      const status = agent.spawn_status || '—';
      const summary = agent.result_summary || '';
      const colorClass = _iconColor(agent);
      const iconHtml = _renderAgentIcon(agent, '24px');
      const name = _displayName(agent);

      const row = document.createElement('div');
      row.className = 'agent-row';
      row.dataset.agentId = agent.id;

      const card = document.createElement('div');
      card.className = 'agent-card agent-square';
      card.innerHTML = `
        <div class="agent-card-top">
          <div class="agent-card-icon-wrap ${colorClass}">${iconHtml}</div>
          <div class="agent-card-meta">
            <div class="agent-card-name-row">
              <span class="agent-card-name">${_esc(name)}</span>
              <span class="agent-clones-status">${_esc(status)}</span>
            </div>
            ${summary ? `<div class="agent-card-desc">${_esc(summary.length > 120 ? summary.slice(0, 120) + '…' : summary)}</div>` : ''}
          </div>
        </div>`;
      row.appendChild(card);
      container.appendChild(row);
    }
  }
}

// ── Squares carousel wiring ───────────────────────────────────────────────────

// ╔═╗ CAROUSEL-WIRING PATTERN (1st of 4 copies)  ═══════════════════════════════════╗
// ║ Native scroll + chevron affordance. Sisters: _wireTabCarousel below,         ║
// ║ _wirePromptCarousel (tab-prompts.js), _wireSkillsCarousel (claude-skills.js). ║
// ║ If you fix scroll or affordance logic, update ALL FOUR.                      ║
// ╚════════════════════════════════════════════════════════════════════════════════╝
function _wireSquaresCarousel(wrap) {
  const scroller = wrap.querySelector('.agents-squares');
  if (!scroller) return;
  const chevLeft  = wrap.querySelector('.agents-carousel-chev.left');
  const chevRight = wrap.querySelector('.agents-carousel-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd   = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };

  scroller.addEventListener('scroll', updateAffordances, { passive: true });
    applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  setTimeout(updateAffordances, 120);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(updateAffordances).observe(scroller);
  }

  const page = () => Math.max(scroller.clientWidth * 0.7, 132);
  if (chevLeft)  chevLeft.addEventListener('click',  e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left:  page(), behavior: 'smooth' }); });
}

// ── Tab carousel wiring ──────────────────────────────────────────────────────

// ╔═╗ CAROUSEL-WIRING PATTERN (2nd of 4 copies — sister of _wireSquaresCarousel) ═╗
// ║ Native scroll + chevron affordance. Differences: CSS selectors only.          ║
// ║ Mirror fixes across all four copies (see the 1st-copy banner above).          ║
// ╚════════════════════════════════════════════════════════════════════════════════╝
function _wireTabCarousel(wrap) {
  if (wrap.dataset.carouselWired === '1') return;
  wrap.dataset.carouselWired = '1';
  const scroller = wrap.querySelector('.agent-card-tabs');
  if (!scroller) return;
  const chevLeft  = wrap.querySelector('.agent-card-tabs-chev.left');
  const chevRight = wrap.querySelector('.agent-card-tabs-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd   = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };

  scroller.addEventListener('scroll', updateAffordances, { passive: true });
    applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(updateAffordances).observe(scroller);
  }

  const page = () => Math.max(scroller.clientWidth * 0.7, 120);
  if (chevLeft)  chevLeft.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left: page(), behavior: 'smooth' }); });
}

// ── Selection ─────────────────────────────────────────────────────────────────

// The session table is a shared DOM node. Start its agent-scoped transition in the
// pointer click itself, before _selectAgent() rebuilds the agent panel. This makes
// the previous agent's rows disappear in the same event turn rather than after a
// broader render or the periodic sessions refresh gets a chance to run.
function _activateAgentFromUserClick(agent) {
  if (!agent || _isMockAgent(agent) || _expandedAgents.has(agent.id)) return;
  setSessionsAgentContext(agent.id, null, { agentName: agent.name || 'Agent' });
  _selectAgent(agent);
}

// A stored tab may still be one of the legacy aliases older builds wrote, and
// 'tools' is no longer a tab at all. Returns null for anything unusable so the
// caller can fall back rather than render a blank body.
function _canonicalTabKey(key) {
  if (key === 'test') return 'agent-loop';
  if (key === 'connections') return 'abilities';
  if (!key || key === 'tools') return null;
  return key;
}

// Tabs the detail panel can always render for any agent, favorite or not — this
// is exactly what the row's ⋮ menu offers, so remembering one of them is always
// safe to restore.
const _ALWAYS_AVAILABLE_TABS = new Set([
  'sessions', 'config', 'prompts', 'agent-loop', 'abilities', 'members', 'monetization', 'automations',
]);

// A stored tab is restorable when it is a REAL page for this agent. Favorites are
// deliberately not required: a page that is open but unstarred renders as an
// EPHEMERAL tab at the end of the header (see _populateAgentTabBar), so restoring
// one can no longer leave the strip with no active tab. Pass the agent to validate
// against its own header set; omit it to accept only the always-available set.
function _restorableTab(tab, agent = null) {
  const key = _canonicalTabKey(tab);
  if (!key) return null;
  if (_ALWAYS_AVAILABLE_TABS.has(key)) return key;
  if (agent && new Map(_agentHeaderTabs(agent)).has(key)) return key;
  return null;
}

function _selectAgent(agent) {
  // An active agent remains open when its tile is clicked again. Closing or
  // removing agents is handled by its dedicated controls, not tile selection.
  if (_expandedAgents.has(agent.id)) return;

  const startedAt = typeof performance === 'undefined' ? 0 : performance.now();
  // Reopen on THIS agent's own last sub-page, so clicking a row lands where that
  // agent was left instead of resetting it to Model. An unstarred page still
  // restores — it shows an ephemeral header tab — so Model is the fallback only
  // for an unknown id (see _restorableTab).
  const tab = _restorableTab(_agentTabMemory.get(agent.id), agent) || 'model';
  _setActive(agent.id, tab);
  // Keep the address in sync with the row click. The carousel and the row ⋮ menu
  // already do this; without it the route keeps naming the PREVIOUS agent, and the
  // next route re-apply (app.refreshAgentsOrder or an agent lifecycle event) snaps
  // the view back — so the click looks like it "didn't switch".
  if (!_isMockAgent(agent)) {
    replaceRouteParams({ agent: agent.id, agentView: tab === 'model' ? null : tab });
  }
  _renderList();
  _saveViewState();
  const section = document.getElementById('sessions-section');
  if (section && startedAt) {
    section.dataset.agentFilterDurationMs = (performance.now() - startedAt).toFixed(1);
    section.dataset.agentFilterId = agent.id;
  }
}

// Start a brand-new chat with this agent in the chat side-panel. Reveals the chat
// panel if it's hidden, then hands off to the chat module's `switchToAgent`
// (set in ui/chat/js/session-init.js) with forceNewSession so the user
// lands in a fresh session bound to this agent. Wired to the chat-message button
// on both the grid square and the expanded agent card.
function _startChatWithAgent(agent) {
  if (!agent || _isMockAgent(agent)) return;
  try {
    if (typeof window.__getChatVisible === 'function' && !window.__getChatVisible()
        && typeof window.__applyChatVisible === 'function') {
      window.__applyChatVisible(true);
    }
  } catch (_) { /* visibility helpers live in index.html — best-effort */ }
  try {
    if (typeof app.switchToAgent === 'function') {
      app.switchToAgent(agent.id, { forceNewSession: true });
    }
  } catch (e) {
    console.warn('agents: start chat with agent failed', e);
  }
}

// ── Surgical square updates ───────────────────────────────────────────────────

function _squaresContainer() {
  return document.querySelector('.agents-squares');
}

function _squareRow(agentId) {
  return document.querySelector(`.agents-squares > .agent-row[data-agent-id="${agentId}"]`);
}

// Repaint an agent's status dot(s) from the live running state — used by the
// `agent_status` WS handler so a run start/stop lights or clears the dot without
// re-rendering the whole grid. Covers both the grid square and, when this agent
// is the open one, its expanded detail card.
export function _applyAgentRunStatus(agentId) {
  if (!agentId) return;
  const running = _isAgentRunning(agentId);
  const dots = [];
  const row = _squareRow(agentId);
  if (row) dots.push(...row.querySelectorAll('.agent-status-dot'));
  if (_activeId() === agentId) {
    const region = document.getElementById('agents-detail-region');
    if (region) dots.push(...region.querySelectorAll('.agent-status-dot'));
  }
  for (const dot of dots) dot.classList.toggle('running', running);
}

function _surgicalAddSquare(agent, opts = {}) {
  const container = _squaresContainer();
  if (!container) return;
  if (_squareRow(agent.id)) return;

  _setAgents([..._agents, agent]);

  _renderSquare(container, agent, _activeId() === agent.id);
  const newRow = container.lastElementChild;
  if (newRow) {
    if (opts.insertBefore && opts.insertBefore !== newRow) {
      container.insertBefore(newRow, opts.insertBefore);
    }
    newRow.classList.add('agent-enter');
    newRow.addEventListener('animationend', () => {
      newRow.classList.remove('agent-enter');
    }, { once: true });
  }
  _syncCarouselState();
}

function _surgicalUpdateSquare(agent) {
  const idx = _agents.findIndex(a => a.id === agent.id);
  if (idx !== -1) _setAgents(_agents.map((a, i) => i === idx ? agent : a));

  const row = _squareRow(agent.id);
  if (!row) return;

  const fragment = document.createElement('div');
  _renderSquare(fragment, agent, _activeId() === agent.id);
  const newRow = fragment.firstElementChild;
  const newCard = newRow ? newRow.querySelector('.agent-card') : null;
  if (!newCard) return;

  const oldCard = row.querySelector('.agent-card');
  if (oldCard) oldCard.replaceWith(newCard);
  else row.prepend(newCard);

  row.classList.remove('agent-pulse');
  void row.offsetWidth;
  row.classList.add('agent-pulse');
  row.addEventListener('animationend', () => { row.classList.remove('agent-pulse'); }, { once: true });

  if (_activeId() === agent.id) _rebuildAgentCard(agent);
}

function _rebuildDetailRegion() {
  const region = document.getElementById('agents-detail-region');
  if (!region) return;
  region.innerHTML = '';
  const activeId = _activeId();
  if (!activeId) { region.classList.remove('open'); _syncSquareActivation(); return; }
  region.classList.add('open');
  if (activeId === MOCK_AGENT_ID) _renderAgentCard(region, _createMockAgent());
  else { const agent = _agents.find(a => a.id === activeId); if (agent) _renderAgentCard(region, agent); }
  _syncSquareActivation();
}

function _syncSquareActivation() {
  const activeId = _activeId();
  document.querySelectorAll('.agents-squares .agent-row').forEach(row => {
    const card = row.querySelector('.agent-card');
    if (!card) return;
    const aid = row.dataset.agentId;
    const isAllAgents = row.dataset.view === 'all-agents';
    card.classList.toggle('activated', isAllAgents ? !activeId : aid === activeId);
  });
}

function _rebuildAgentCard(agent) {
  const region = document.getElementById('agents-detail-region');
  if (!region) return;
  region.innerHTML = '';
  region.classList.add('open');
  _renderAgentCard(region, agent);
  _syncSquareActivation();
}

function _syncCarouselState() {
  const grid = document.getElementById('agents-grid');
  if (!grid) return;
  const activeId = _activeId();
  const allAgentsActive = !_binView && !_clonesView && !activeId;
  grid.classList.toggle('carousel', !!activeId || allAgentsActive);
  const wrap = grid.querySelector('.agents-squares-wrap');
  if (wrap) _wireSquaresCarousel(wrap);
  _syncMergedLayout();
}

// ── Merged sessions layout ───────────────────────────────────────────────────
// The Agents page embeds the Sessions table below the squares strip (see
// agents.html #sessions-section). The section is visible only in the normal
// overview — not the bin, not clones, and not while an agent card is open. The
// class on #tab-agents lets the CSS switch the two layouts atomically.
function _syncMergedLayout() {
  const tab = document.getElementById('tab-agents');
  if (!tab) return;
  // Phantom takeover: drop the static shell's .agents-phantom markers so the
  // REAL merged-layout CSS (#tab-agents.agents-show-sessions) governs from here
  // on — and, crucially, so the sessions section hides again when a card opens.
  const _grid = document.getElementById('agents-grid');
  if (_grid) _grid.classList.remove('agents-phantom');
  const _section = document.getElementById('sessions-section');
  if (_section) _section.classList.remove('agents-phantom');
  // The account-wide sessions table now lives inside the All Agents Sessions
  // tab, so the old separate merged overview is retired.
  const merged = false;
  tab.classList.toggle('agents-show-sessions', merged);
  if (merged) {
    _wireMergedScroll();      // one-time listeners (idempotent)
    _measureMergedScroll();   // (re)measure the table height + (re)apply the lock
  } else {
    tab.classList.remove('sessions-locked');
  }
}

// ── Merged sessions two-phase scroll ─────────────────────────────────────────
// Overview scroll happens in two phases so the overscroll (rubber-band) stays on
// the data table rather than the whole page:
//   1. The page (#tab-agents) scrolls the agents strip off the top while the
//      sessions toolbar sticks (position:sticky, top:0).
//   2. Once the strip is gone we flip .sessions-locked: the table wrap becomes
//      its own scroller and the wheel is routed to it. Scrolling back up at the
//      table's top hands the wheel back to the page so the strip can return.
let _mergedScrollWired = false;

function _measureMergedScroll() {
  const tab = document.getElementById('tab-agents');
  if (!tab || !tab.classList.contains('agents-show-sessions')) return;
  const toolbar = document.getElementById('sessions-toolbar');
  const wrap = document.getElementById('sessions-table-wrap');
  if (toolbar && wrap && tab.clientHeight > 0) {
    const h = tab.clientHeight - toolbar.offsetHeight;
    if (h > 0) wrap.style.height = h + 'px';
  }
  const grid = document.getElementById('agents-grid');
  if (grid) {
    tab.classList.toggle('sessions-locked', tab.scrollTop >= grid.offsetHeight - 2);
  }
  _syncTableOverscroll();
}

// Touch handoff for the table scroller. In phase 1 (not locked) the table wrap
// has touch-action: pan-x, so a vertical swipe inside it chains to the PAGE and
// pushes the strip away. Once locked, the table owns vertical scrolling; we keep
// overscroll contained at the bottom (rubber-band stays in the table) but relax
// it to `auto` at the table's top so a downward swipe chains back to the page and
// reveals the strip again — mirroring the wheel routing in _wireMergedScroll.
function _syncTableOverscroll() {
  const tab = document.getElementById('tab-agents');
  const wrap = document.getElementById('sessions-table-wrap');
  if (!tab || !wrap) return;
  if (!tab.classList.contains('sessions-locked')) {
    wrap.style.overscrollBehaviorY = '';   // phase 1: CSS default (auto) chains to page
    return;
  }
  wrap.style.overscrollBehaviorY = (wrap.scrollTop <= 1) ? 'auto' : 'contain';
}

function _wireMergedScroll() {
  if (_mergedScrollWired) return;
  const tab = document.getElementById('tab-agents');
  const toolbar = document.getElementById('sessions-toolbar');
  const wrap = document.getElementById('sessions-table-wrap');
  if (!tab || !toolbar || !wrap) return;
  _mergedScrollWired = true;

  tab.addEventListener('scroll', _measureMergedScroll, { passive: true });
  wrap.addEventListener('scroll', _syncTableOverscroll, { passive: true });
  window.addEventListener('resize', _measureMergedScroll);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(_measureMergedScroll).observe(tab);
    new ResizeObserver(_measureMergedScroll).observe(toolbar);
  }

  // Route the wheel between the page scroller and the table scroller.
  wrap.addEventListener('wheel', (e) => {
    let dx = e.deltaX || 0;
    let dy = e.deltaY || 0;
    if (e.deltaMode === 1) { dx *= 20; dy *= 20; }
    else if (e.deltaMode === 2) { dx *= wrap.clientHeight; dy *= wrap.clientHeight; }
    if (Math.abs(dx) > Math.abs(dy)) return;  // horizontal intent → native

    const locked = tab.classList.contains('sessions-locked');
    if (!locked) {
      // Strip still visible: the PAGE owns vertical wheel — scroll it.
      tab.scrollTop += dy;
      _measureMergedScroll();
      e.preventDefault();
    } else if (wrap.scrollTop <= 0 && dy < 0) {
      // Table pinned at its top, scrolling up: hand back to the page so the
      // strip can return into view.
      tab.scrollTop += dy;
      _measureMergedScroll();
      e.preventDefault();
    }
  }, { passive: false });

  requestAnimationFrame(_measureMergedScroll);
  setTimeout(_measureMergedScroll, 120);
}

// ── Agent card (expanded) ────────────────────────────────────────────────────

function _renderAgentCard(grid, agent) {
  const isMock = _isMockAgent(agent);
  const anonymousCreatePreview = isMock && _isAnonymousCreatePreview();

  // Local Claude Code agents get an entirely separate, tab-less card.
  if (!isMock && _isClaudeAgent(agent)) { _renderClaudeAgentCard(grid, agent); return; }
  if (!isMock && _isCodexAgent(agent)) { _renderCodexAgentCard(grid, agent); return; }

  // Terminal Chat agents get an entirely separate, stripped card.
  if (!isMock && _isTerminalChatAgent(agent)) { _renderTerminalChatAgentCard(grid, agent); return; }

  // For the mock create card, the chosen type (WebAgent / Claude / Terminal) drives
  // the header icon + hint, and — for the two engine types — replaces the tabbed
  // lower area with an inline engine settings form (built below).
  const mockType = isMock ? _mockCreateType() : 'webagent';
  const engineMock = isMock && mockType !== 'webagent';

  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-tabs-only' + (isMock ? ' agent-card-mock' : '');

  const mockIconColor = mockType === 'claude' ? 'color-claude'
    : mockType === 'codex' ? 'color-codex'
    : mockType === 'terminal' ? 'color-terminal' : 'color-blue';
  const mockIconHtml = mockType === 'webagent' && agent.icon ? icon(agent.icon, { size: '30px' })
    : mockType === 'claude' ? claudeMark({ size: '30px' })
    : mockType === 'codex' ? codexMark({ size: '30px' })
    : mockType === 'terminal' ? icon('terminal', { size: '30px' })
    : icon(_mockRandomIcon(), { size: '30px' });
  const mockPlaceholder = anonymousCreatePreview ? 'Register an account to name and create an agent…'
    : mockType === 'claude' ? 'Name this Claude agent…'
    : mockType === 'terminal' ? 'Name this Terminal agent…'
    : 'Create a new agent…';
  const mockHint = anonymousCreatePreview
    ? 'Preview the agent configuration below. Register or sign in when you are ready to create it.'
    : mockType === 'claude' ? 'Sign in to Claude and set a working folder, then hit +'
    : mockType === 'terminal' ? 'Set the command to run, then hit +'
    : 'Type a name (pick a template below), then hit +';

  // Engine create forms own their full lower area (no tab strip).
  const tabsWrapHtml = `
    <div class="agent-card-tabs-wrap">
      <button type="button" class="agent-card-tabs-chev left" aria-label="Scroll tabs left" tabindex="-1">&#10094;</button>
      <div class="agent-card-tabs" role="tablist"></div>
      <button type="button" class="agent-card-tabs-chev right" aria-label="Scroll tabs right" tabindex="-1">&#10095;</button>
    </div>`;

  // The draft header belongs to the detail page, not to the outer card chrome.
  // WebAgent keeps its tab strip in the card; engine forms deliberately keep their
  // existing no-tabs surface and receive the same header inside their form panel.
  card.innerHTML = isMock ? (engineMock ? '' : tabsWrapHtml) : tabsWrapHtml;

  const buildMockCreateHeader = () => {
    const header = document.createElement('section');
    header.className = 'agent-mock-create-header';
    header.innerHTML = `
      <div class="agent-card-top">
        <div class="agent-card-icon-wrap ${mockIconColor}">${mockIconHtml}</div>
        <div class="agent-card-meta">
          <div class="agent-card-name-row">
            <div class="agent-mock-create-field">
              <div class="agent-mock-name-input" contenteditable="true" role="textbox"
                   aria-label="New agent name" data-placeholder="${_esc(mockPlaceholder)}"
                   spellcheck="false"></div>
            </div>
          </div>
          <div class="agent-card-desc agent-card-mock-hint">${_esc(mockHint)}</div>
        </div>
        <div class="agent-card-badge-wrap">
          <button type="button" class="agent-mock-create-go${anonymousCreatePreview ? ' registration-gated' : ''}"
                  title="${anonymousCreatePreview ? 'Register or sign in to create an agent' : 'Create agent'}"
                  aria-label="${anonymousCreatePreview ? 'Register or sign in to create an agent' : 'Create agent'}"
                  ${anonymousCreatePreview ? 'aria-disabled="true" aria-haspopup="dialog"' : ''}>${icon('plus', { size: '22px' })}</button>
        </div>
      </div>`;
    if (_userIsAdmin) header.appendChild(_buildMockTypeToggle(mockType));
    return header;
  };

  const row = document.createElement('div');
  row.className = 'agent-row expanded';
  row.dataset.agentId = agent.id;

  let panel = null;
  if (engineMock) {
    // Claude / Codex / Terminal retain their focused inline form; only the shared
    // name/create header moves into that form panel.
    panel = _buildEngineCreatePanel(agent, mockType);
  } else {
    const state = _expandedAgents.get(agent.id);
    if (state) panel = _buildDetailPanel(agent);
  }

  if (!engineMock) row.appendChild(card);
  if (panel) row.appendChild(panel);

  if (isMock && panel) {
    const content = panel.querySelector(':scope > .agent-detail-content');
    const body = panel.querySelector(':scope > .agent-detail-content > .agent-detail-body');
    const header = buildMockCreateHeader();
    if (content) content.insertBefore(header, body || content.firstChild);
    _wireMockCreateField(header, () => _acceptMockCreate(header));
  }

  if (!engineMock) {
    const cardTabBar = card.querySelector('.agent-card-tabs');
    if (cardTabBar) _populateAgentTabBar(cardTabBar, agent, panel);
    const cardTabWrap = card.querySelector('.agent-card-tabs-wrap');
    if (cardTabWrap) _wireTabCarousel(cardTabWrap);
  }

  grid.appendChild(row);
}

// ── Mock create card: type chooser, engine forms, accept dispatch ──────────────

function _isAnonymousCreatePreview() {
  return isAnonGuest() || String(app.currentUserId || '').startsWith('anon_');
}

function _showMockRegistrationGate(card) {
  document.querySelectorAll('.agent-create-registration-popover').forEach(el => el.remove());
  const button = card.querySelector('.agent-mock-create-go');
  if (!button) return;

  const popover = document.createElement('div');
  popover.className = 'agent-create-registration-popover';
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-label', 'Registration required');
  popover.innerHTML = `
    <strong>Register to create this agent</strong>
    <span>Anonymous visitors can explore the configuration form, but only registered accounts can create and manage agents.</span>
    <div class="agent-create-registration-actions">
      <button type="button" class="agent-create-registration-dismiss">Not now</button>
      <button type="button" class="agent-create-registration-open">Register or sign in</button>
    </div>`;

  const badge = button.closest('.agent-card-badge-wrap') || card;
  badge.appendChild(popover);
  popover.querySelector('.agent-create-registration-dismiss')?.addEventListener('click', e => {
    e.stopPropagation();
    popover.remove();
    button.focus();
  });
  popover.querySelector('.agent-create-registration-open')?.addEventListener('click', e => {
    e.stopPropagation();
    popover.remove();
    showLeftOverlay();
  });
}

// The segmented WebAgent / Claude / Terminal toggle at the top of the create card.
// Clicking a segment records the chosen type and re-renders the detail region in
// place (the typed name persists via the mock draft state in mock-agent.js).
function _buildMockTypeToggle(activeType) {
  const seg = document.createElement('div');
  seg.className = 'agent-mock-type-seg';
  seg.addEventListener('click', e => e.stopPropagation());
  const opts = [
    ['webagent', 'WebAgent', icon('sparkles', { size: '14px' })],
    ['claude', 'Claude', claudeMark({ size: '14px' })],
    ['codex', 'Codex', codexMark({ size: '14px' })],
    ['terminal', 'Terminal', icon('terminal', { size: '14px' })],
  ];
  for (const [key, label, ic] of opts) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'agent-mock-type-seg-btn' + (key === activeType ? ' active' : '');
    b.dataset.type = key;
    b.innerHTML = `${ic}<span>${label}</span>`;
    b.addEventListener('click', e => {
      e.stopPropagation();
      if (_mockCreateType() === key) return;
      _setMockCreateType(key);
      _rebuildDetailRegion();
    });
    seg.appendChild(b);
  }
  return seg;
}

// The inline engine (Claude / Terminal) create form panel. Reuses the Claude card's
// panel skin; stashes the form's `collect()` on the panel so the accept handler can
// read the chosen settings.
function _buildEngineCreatePanel(agent, type) {
  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel agent-detail-panel-claude agent-detail-panel-mock-engine';
  panel.dataset.agentId = agent.id;
  const content = document.createElement('div'); content.className = 'agent-detail-content';
  const body = document.createElement('div'); body.className = 'agent-detail-body';
  content.appendChild(body); panel.appendChild(content);
  const drafts = _mockEngineDraft();
  panel._collect = type === 'claude' ? renderClaudeCreateBody(body, drafts.claude)
    : type === 'codex' ? renderCodexCreateBody(body, drafts.codex)
    : renderTerminalChatCreateBody(body, drafts.terminal);
  return panel;
}

// The "+" accept button finalises the create form, dispatching on the chosen type.
function _acceptMockCreate(card) {
  if (_isAnonymousCreatePreview()) {
    _showMockRegistrationGate(card);
    return;
  }
  const type = _mockCreateType();
  if (type === 'claude' || type === 'codex' || type === 'terminal') return _acceptEngineCreate(card, type);
  return _acceptWebAgentCreate(card);
}

async function _acceptWebAgentCreate(card) {
  const input = card.querySelector('.agent-mock-name-input');
  const name = (_mockDraftName() || (input ? input.textContent : '') || '').trim();
  if (!name) { if (input) input.focus(); return; }
  // Template + description come from the Config tab (the WebAgent lower area); the
  // mock has no per-agent model UI, so it always inherits the app default model.
  const row = card.closest('.agent-row');
  const panel = row ? row.querySelector('.agent-detail-panel') : null;
  const tplSel = panel ? panel.querySelector('[data-field="template"]') : null;
  const descEl = panel ? panel.querySelector('[data-field="desc"]') : null;
  const config = _mockAgentConfigPayload();
  const description = descEl ? descEl.value.trim() : (config.description || '');
  delete config.description;
  const selectedAbilities = _mockAbilityConnections().filter(connection => connection.enabled);
  const goBtn = card.querySelector('.agent-mock-create-go');
  if (goBtn) goBtn.disabled = true;
  try {
    const newAgent = await _postNewAgent({
      name,
      description,
      templateId: tplSel ? tplSel.value : 'default',
      config,
    });
    if (newAgent && selectedAbilities.length) {
      const results = await Promise.allSettled(selectedAbilities.map(async ({ connection_type, config: abilityConfig }) => {
        const response = await fetch(`/api/v1/agents/${encodeURIComponent(newAgent.id)}/connections/${encodeURIComponent(connection_type)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ user_id: app.currentUserId, enabled: true, config: abilityConfig || {} }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.detail || `${connection_type} could not be enabled`);
        }
      }));
      const failures = results.filter(result => result.status === 'rejected');
      if (failures.length) {
        const message = `Agent created, but ${failures.length} selected ${failures.length === 1 ? 'ability was' : 'abilities were'} not applied.`;
        if (typeof window.__agentsNotice === 'function') window.__agentsNotice(message);
        else alert(message);
      }
    }
  } catch (e) {
    console.warn('agents: create agent failed', e);
    alert('Error creating agent: ' + e.message);
    if (goBtn) goBtn.disabled = false;
  }
}

// Create a Claude / Terminal engine agent: clone its template, then apply the
// inline form's draft settings in one silent PUT and re-render the (now distinct)
// engine card. The name falls back to a non-colliding default when left blank.
async function _acceptEngineCreate(card, type) {
  const row = card.closest('.agent-row');
  const panel = row ? row.querySelector('.agent-detail-panel') : null;
  const collect = (panel && typeof panel._collect === 'function') ? panel._collect : null;
  const settings = collect ? collect() : null;
  const input = card.querySelector('.agent-mock-name-input');
  const typed = (_mockDraftName() || (input ? input.textContent : '') || '').trim();
  const name = typed || (type === 'claude' ? _defaultClaudeName() : type === 'codex' ? _defaultCodexName() : _defaultTerminalChatName());
  const templateId = type === 'claude' ? 'local-claude' : type === 'codex' ? 'local-codex' : 'terminal-chat';
  const goBtn = card.querySelector('.agent-mock-create-go');
  if (goBtn) { goBtn.disabled = true; goBtn.innerHTML = icon('loader-2', { size: '22px' }); }
  try {
    const newAgent = await _postNewAgent({ name, templateId });
    if (newAgent && settings) {
      const updates = type === 'claude'
        ? { claude_code: settings.claude_code, default_execution_mode: settings.default_execution_mode }
        : type === 'codex' ? { codex_code: settings.codex_code } : { terminal_chat: settings.terminal_chat };
      await _putAgentField(newAgent, updates, null, { silent: true });
      if (typeof window.__agentsRebuildDetailRegion === 'function') window.__agentsRebuildDetailRegion();
    }
  } catch (e) {
    console.warn('agents: create engine agent failed', e);
    alert('Error creating agent: ' + e.message);
    if (goBtn) { goBtn.disabled = false; goBtn.innerHTML = icon('plus', { size: '22px' }); }
  }
}

// ── Local engine agent tabs ───────────────────────────────────────────────────
// Persisted engine agents use the same tabs-only chrome as WebAgents. Their
// engine-specific Config bodies are mounted by the corresponding module.
function _renderClaudeAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

  const row = document.createElement('div');
  row.className = 'agent-row expanded';
  row.dataset.agentId = agent.id;
  row.appendChild(card);

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel agent-detail-panel-claude';
  panel.dataset.agentId = agent.id;
  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);
  panel.appendChild(content);
  row.appendChild(panel);

  // Slim two-tab bar (Settings | Skills) lives inside the card; it swaps the panel
  // body between the Claude settings and the Skills tab (claude-skills.js).
  const tabBar = card.querySelector('.agent-card-tabs');
  mountClaudeCardTabs(tabBar, body, agent);

  grid.appendChild(row);
}

// Codex is an alternate headless CLI engine too, but it deliberately keeps a
// smaller card than Claude: credentials are managed by Codex itself, so the only
// per-agent controls are its working folder, model, and optional CLI flags.
function _renderCodexAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;
  const row = document.createElement('div'); row.className = 'agent-row expanded'; row.dataset.agentId = agent.id; row.appendChild(card);
  const panel = document.createElement('div'); panel.className = 'agent-detail-panel agent-detail-panel-claude';
  const content = document.createElement('div'); content.className = 'agent-detail-content';
  const body = document.createElement('div'); body.className = 'agent-detail-body'; content.appendChild(body); panel.appendChild(content); row.appendChild(panel);
  const tabBar = card.querySelector('.agent-card-tabs');
  mountCodexCardTabs(tabBar, body, agent);
  grid.appendChild(row);
}

// Terminal Chat keeps its engine-specific Config body under the shared tabs-only
// chrome used by the other persisted agent types.
function _renderTerminalChatAgentCard(grid, agent) {
  const card = document.createElement('div');
  card.className = 'agent-card active agent-card-claude agent-card-tabs-only';
  card.innerHTML = `
    <div class="agent-card-tabs-wrap">
      <div class="agent-card-tabs" role="tablist"></div>
    </div>`;

  const row = document.createElement('div');
  row.className = 'agent-row expanded';
  row.dataset.agentId = agent.id;
  row.appendChild(card);

  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel agent-detail-panel-claude';
  panel.dataset.agentId = agent.id;
  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);
  panel.appendChild(content);
  row.appendChild(panel);

  // Slim tab bar lives above the panel body.
  const tabBar = card.querySelector('.agent-card-tabs');
  mountTerminalChatCardTabs(tabBar, body, agent);

  grid.appendChild(row);
}

// ── Tab bar ────────────────────────────────────────────────────────────────────

// Id of the header tab currently being dragged. Held in module state as well as
// dataTransfer so the reorder survives even where dataTransfer payloads are
// restricted between same-page elements.
let _draggingTabId = null;

// Reproject a new global tab order onto every rendered header strip in place.
// Reordering the EXISTING buttons (rather than re-rendering the card) keeps the
// open panel body, its scroll position and the active tab intact — the same
// no-remount discipline the star toggle uses.
function _syncTabBarOrder(ids) {
  document.querySelectorAll('.agent-card-tabs').forEach((bar) => {
    // The My Agents (All Agents) strip carries a different id set and has its own
    // order helper (_syncAllAgentsTabOrder); never reorder it from here.
    if (bar.closest('.agent-row-all-agents')) return;
    const buttons = new Map();
    bar.querySelectorAll(':scope > .agents-detail-tab').forEach((btn) => buttons.set(btn.dataset.tab, btn));
    ids.forEach((id) => {
      const btn = buttons.get(id);
      if (btn) bar.appendChild(btn);   // appendChild MOVES an existing node, in order
    });
    // Keep the ephemeral (not-favorited) tab pinned LAST — it is absent from `ids`,
    // so the reorder above would otherwise shuffle it to the front.
    const ephemeral = bar.querySelector(':scope > .agents-detail-tab.ephemeral');
    if (ephemeral) bar.appendChild(ephemeral);
  });
}

// The full header page set for one agent, BEFORE favorites filter it. Ephemeral
// (opened-but-not-favorited) tabs are validated against this list, so an unknown
// id can never render as an empty tab.
function _agentHeaderTabs(agent) {
  const isMock = _isMockAgent(agent);
  const configTopicTabs = _CONFIG_TOPIC_PAGES.map((page) => [page.id, page.tabLabel || page.title]);
  const tabs = isMock
    ? [['config', 'Config'], ...configTopicTabs, ['abilities', 'Abilities'], ['automations', 'Automations']]
    : [['sessions', 'Sessions'], ['config', 'Config'], ...configTopicTabs, ['prompts', 'Prompts'], ['agent-loop', 'Agent Loop'], ['abilities', 'Abilities']];
  if (!isMock) {
    if (_userIsAdmin) tabs.push(['members', 'Members']);
    tabs.push(['monetization', 'Monetization'], ['automations', 'Automations']);
  }
  return tabs;
}

function _populateAgentTabBar(tabBar, agent, panel) {
  const state = _expandedAgents.get(agent.id);
  const isMock = _isMockAgent(agent);
  const activeTab = state ? state.tab : null;
  tabBar.innerHTML = '';

  // Favorite pages determine both visible tabs and their user-chosen order.
  // Config is required by the preference helper, but it is not pinned.
  const tabsById = new Map(_agentHeaderTabs(agent));
  const favorites = readAgentPageFavorites();
  const headerTabs = favorites
    .map((pageId) => [pageId, tabsById.get(pageId)])
    .filter(([, tab]) => Boolean(tab));

  // An open page that is NOT a favorite still gets a tab, pinned to the very END
  // of the header. It is EPHEMERAL: recomputed from the active tab on every render,
  // never persisted, never part of the saved order, and gone as soon as the user
  // navigates away from that page.
  const ephemeralId = (activeTab && tabsById.has(activeTab) && !favorites.includes(activeTab))
    ? activeTab
    : null;
  if (ephemeralId) headerTabs.push([ephemeralId, tabsById.get(ephemeralId)]);

  for (const [key, label] of headerTabs) {
    const isEphemeral = key === ephemeralId;
    const btn = document.createElement('button');
    btn.className = 'agents-detail-tab'
      + (isEphemeral ? ' ephemeral' : '')
      + (activeTab === key ? ' active' : '');
    btn.dataset.tab = key;
    if (isEphemeral) btn.title = 'Temporary tab — this page is not a favorite';
    if (key === 'abilities') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else if (key === 'members') {
      btn.innerHTML = `${label} <span class="tab-count-badge tab-count-badge-pending">…</span>`;
    } else {
      btn.textContent = label;
    }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _setActive(agent.id, key);
    replaceRouteParams({ agent: agent.id, agentView: key === 'model' ? null : key });
      _renderList();
      _saveViewState();
    });

    // ── Drag-to-reorder header tabs ───────────────────────────────────────────
    // The header order IS this user's global favorite order, so a drop rewrites
    // the shared preference (and its server record), not a per-card list. Mirrors
    // the reorder convention in app-settings.js: `.dragging` on the source,
    // `.drag-over` on the target, splice-move on drop. Deliberate divergences: the
    // strip is horizontal (so the drop marker is a leading edge bar), the ordered
    // list is global, and the tab itself is the drag handle (no separate grip).
    // Ephemeral tabs sit OUTSIDE the saved order, so they are not draggable — a
    // drop would splice an id into an order it was never part of.
    btn.draggable = !isEphemeral;
    btn.addEventListener('dragstart', (e) => {
      _draggingTabId = key;
      btn.classList.add('dragging');
      try {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', key);
      } catch (_) { /* dataTransfer restricted — the module var still carries it */ }
    });
    btn.addEventListener('dragend', () => {
      _draggingTabId = null;
      btn.classList.remove('dragging');
      tabBar.querySelectorAll('.agents-detail-tab').forEach((el) => el.classList.remove('drag-over'));
    });
    btn.addEventListener('dragover', (e) => {
      if (!_draggingTabId || _draggingTabId === key) return;
      // preventDefault on EVERY dragover, or the drop event never fires.
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      tabBar.querySelectorAll('.agents-detail-tab').forEach((el) => el.classList.remove('drag-over'));
      btn.classList.add('drag-over');
    });
    btn.addEventListener('dragleave', () => btn.classList.remove('drag-over'));
    btn.addEventListener('drop', (e) => {
      e.preventDefault();
      btn.classList.remove('drag-over');
      const fromId = _draggingTabId;
      _draggingTabId = null;
      const toId = key;
      if (!fromId || fromId === toId) return;      // self-drop is a no-op
      const ids = readAgentPageFavorites();
      const fromIdx = ids.indexOf(fromId);
      if (fromIdx < 0) return;                     // unknown id — leave order alone
      ids.splice(fromIdx, 1);
      const toIdx = ids.indexOf(toId);
      if (toIdx < 0) ids.push(fromId); else ids.splice(toIdx, 0, fromId);
      // One writer for both the UI mirror and the authoritative server record.
      const next = writeAgentPageFavorites(ids);
      _syncTabBarOrder(next);
    });

    tabBar.appendChild(btn);
  }

  if (isMock) return;

  _fetchAbilitiesAndTools(agent).then(({ abilitiesCount }) => {
    const connBtn = tabBar.querySelector('.agents-detail-tab[data-tab="abilities"]');
    if (connBtn) connBtn.innerHTML = `Abilities <span class="tab-count-badge">${abilitiesCount}</span>`;
  }).catch(() => {
    const connBtn = tabBar.querySelector('.agents-detail-tab[data-tab="abilities"]');
    if (connBtn) connBtn.innerHTML = `Abilities <span class="tab-count-badge">0</span>`;
  });

  _fetchMembersCount(agent).then(count => {
    const memBtn = tabBar.querySelector('.agents-detail-tab[data-tab="members"]');
    if (memBtn) memBtn.innerHTML = `Members <span class="tab-count-badge">${count}</span>`;
  }).catch(() => {
    const memBtn = tabBar.querySelector('.agents-detail-tab[data-tab="members"]');
    if (memBtn) memBtn.innerHTML = `Members <span class="tab-count-badge">0</span>`;
  });
}

// ── Detail panel ──────────────────────────────────────────────────────────────

function _buildDetailPanel(agent) {
  const panel = document.createElement('div');
  panel.className = 'agent-detail-panel';
  panel.dataset.agentId = agent.id;

  const content = document.createElement('div');
  content.className = 'agent-detail-content';
  panel.appendChild(content);

  // The mock create card finalises via the header "+" accept button
  // (view.js _acceptMockCreate), so the panel carries no separate "Create" bar.

  const body = document.createElement('div');
  body.className = 'agent-detail-body';
  content.appendChild(body);

  _renderPanelBody(agent, panel);

  return panel;
}

// ── Panel body (tab content) ──────────────────────────────────────────────────

function _renderPanelBody(agent, panelEl) {
  const state = _expandedAgents.get(agent.id);
  let tab = state?.tab || 'model';

  const row = panelEl.closest('.agent-row');
  if (row) {
    row.querySelectorAll('.agent-card-tabs .agents-detail-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
  }

  const body = panelEl.querySelector('.agent-detail-body');
  if (!body) return;
  // Match the All Agents Sessions tab: its shared footer dock must be active
  // before the movable Sessions section is mounted, including on narrow screens.
  _setAllAgentsSessionsFooterDock(tab === 'sessions');
  // The movable Sessions table is re-scoped to THIS agent on the sessions tab a
  // few lines below. Clearing the context here on that tab would repaint the
  // account-wide table — and its "All Agent's Sessions" title — straight over the
  // agent the user just clicked, so only clear it when leaving the sessions tab.
  if (tab !== 'sessions') setSessionsAgentContext(null);
  _setAutomationsContext(null);
  stopAutomations();
  _destroyAllAgentsModelTable();
  body.innerHTML = '';

  const content = panelEl.querySelector('.agent-detail-content');
  const oldSaveBar = content ? content.querySelector(':scope > .agents-save-bar') : null;
  if (oldSaveBar) oldSaveBar.remove();

  if (tab === 'sessions')          setSessionsAgentContext(agent.id, body, { agentName: agent.name || 'Agent' });
  else if (tab === 'config')       _renderConfigTab(body, agent, panelEl);
  else if (_CONFIG_TOPIC_IDS.has(tab)) _renderConfigTopicTab(body, agent, panelEl, tab);
  else if (tab === 'prompts')      _renderPromptsTab(body, agent, panelEl);
  else if (tab === 'agent-loop')   _renderTestTab(body, agent);
  else if (tab === 'abilities')    _renderConnectionsTab(body, agent);
  else if (tab === 'members')      _renderMembersTab(body, agent);
  else if (tab === 'automations') {
    _setAutomationsContext(body);
    startAutomations();
  }
  else if (tab === 'monetization') {
    import('../pages/monetization/page.js').then(({ renderAgentMonetization }) => {
      if (_expandedAgents.get(agent.id)?.tab === 'monetization') {
        renderAgentMonetization(body, agent.id);
      }
    }).catch((e) => console.error('agents: monetization tab failed', e));
  }
}

// ── Selection / bin / clones toolbar ──────────────────────────────────────────

function _syncSelectionVisuals() {
  document.querySelectorAll('.agents-squares .agent-row').forEach(row => {
    const id = row.dataset.agentId;
    const on = _selectedIds.has(id);
    row.classList.toggle('selected', on);
    const box = row.querySelector('.agent-select-box');
    if (box) box.innerHTML = on ? icon('check', { size: '13px' }) : '';
  });
}

function _toggleSelect(agentId) {
  if (_selectedIds.has(agentId)) _selectedIds.delete(agentId);
  else _selectedIds.add(agentId);
  _syncSelectionVisuals();
  _updateBinToolbar();
}

export function _updateBinToolbar() {
  const toolbar = document.getElementById('agents-toolbar');
  const restoreBtn = document.getElementById('agents-restore-btn');
  const backBtn = document.getElementById('agents-back-btn');
  const n = _selectedIds.size;
  const showToolbar = _binView || _clonesView;

  if (toolbar) toolbar.style.display = showToolbar ? 'flex' : 'none';
  if (backBtn) backBtn.style.display = showToolbar ? 'inline-flex' : 'none';
  if (restoreBtn) {
    restoreBtn.style.display = _binView ? 'inline-flex' : 'none';
    restoreBtn.disabled = (n === 0);
  }
}

export function _bindBinToolbar() {
  const actions = document.getElementById('agents-toolbar-actions');
  if (!actions || actions._bound) return;
  actions._bound = true;

  const restoreBtn = document.getElementById('agents-restore-btn');
  const backBtn = document.getElementById('agents-back-btn');

  if (restoreBtn) restoreBtn.addEventListener('click', () => {
    if (_selectedIds.size > 0) _restoreSelected();
  });
  if (backBtn) backBtn.addEventListener('click', () => { if (_binView) _exitBin(); else if (_clonesView) _exitClones(); });
}


async function _enterBin() {
  if (_binView) return;
  _setBinView(true);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _exitBin() {
  if (!_binView) return;
  _setBinView(false);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _enterClones() {
  if (_clonesView) return;
  _setClonesView(true);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _exitClones() {
  if (!_clonesView) return;
  _setClonesView(false);
  _selectedIds.clear();
  _clearExpanded();
  _renderSkeleton();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
}

async function _restoreSelected() {
  const ids = [..._selectedIds];
  await Promise.all(ids.map(id =>
    fetch(`/api/v1/agents/${id}/restore?user_id=${encodeURIComponent(app.currentUserId)}`, { method: 'POST', headers: { ...authHeaders() } }).catch(() => {})
  ));
  _selectedIds.clear();
  window.__agentsSharedData = null;
  invalidateAgentListCaches();
  await _loadAgents();
  _renderList();
  _updateBinToolbar();
  if (typeof app.populateAgentSelect === 'function') { try { await app.populateAgentSelect(app.currentUserId); } catch (_) {} }
}


// ── View state persistence ────────────────────────────────────────────────────

const _STORAGE_KEY = 'agents_view_state';

// Remember the last sub-page shown PER agent, so switching between agents (and
// reloading) returns each card to where you left it. `_expandedAgents` only ever
// holds the one open agent, so the durable memory lives in this separate map and
// rides in the same local view-state record.
const _agentTabMemory = new Map();

function _saveViewState() {
  const expanded = {};
  for (const [agentId, state] of _expandedAgents) {
    if (agentId === MOCK_AGENT_ID) continue;
    expanded[agentId] = { tab: state.tab };
    // Store canonical keys only, so a legacy alias can never be replayed later.
    _agentTabMemory.set(agentId, _canonicalTabKey(state.tab) || 'model');
  }
  kvWrite('agents:view-state', _STORAGE_KEY, {
    expanded,
    // Per-agent memory: every agent's last sub-page, not just the open one.
    tabs: Object.fromEntries(_agentTabMemory),
    // My Agents (All Agents) card remembers its own last sub-page too.
    allAgentsTab: _allAgentsTab,
  });
}

export function _restoreViewState() {
  try {
    const saved = kvRead('agents:view-state', _STORAGE_KEY);
    if (!saved) return;
    const parsed = typeof saved === 'string' ? JSON.parse(saved) : saved;
    // Seed per-agent memory from the whole tab map so returning to ANY agent
    // reopens the sub-page that agent was last showing.
    if (parsed.tabs && typeof parsed.tabs === 'object') {
      for (const [agentId, tab] of Object.entries(parsed.tabs)) {
        const key = _canonicalTabKey(tab);
        if (key) _agentTabMemory.set(agentId, key);
      }
    }
    // My Agents card restores its own last sub-page. Only an UNKNOWN page is
    // rejected downstream; a not-favorited one restores and shows an ephemeral
    // header tab (see the guard in _renderAllAgentsTabBody).
    if (typeof parsed.allAgentsTab === 'string' && parsed.allAgentsTab) {
      _allAgentsTab = parsed.allAgentsTab;
    }
    const { expanded } = parsed;
    if (!expanded || typeof expanded !== 'object') return;
    let changed = false;
    for (const [agentId, state] of Object.entries(expanded)) {
      const agent = _agents.find(a => a.id === agentId);
      if (agent) {
        // Migrate the old internal keys so persisted card state uses the same
        // canonical IDs as the root Agents page catalog. `tools` is no longer a
        // tab, so it lands on Model like any other unresolved page.
        const legacyTab = state.tab === 'test' ? 'agent-loop'
          : state.tab === 'connections' ? 'abilities'
          : state.tab;
        const requestedTab = (legacyTab && legacyTab !== 'tools') ? legacyTab : 'model';
        // Restore any page VALID for this agent, favorite or not: an unstarred page
        // returns with an ephemeral header tab, so it can no longer restore as an
        // invisible selection. Only an unknown id falls back to Config.
        const tab = _restorableTab(requestedTab, agent) || 'model';
        _agentTabMemory.set(agentId, tab);
        _setActive(agentId, tab);
        changed = true;
        break;
      }
    }
    if (changed) _renderList();
  } catch (_e) { /* corrupted view state — fall back to defaults */ }
}

// ── Lazy imports for tab modules (imported on first render) ───────────────────
// These are dynamic imports so the view module doesn't statically import every
// tab — tabs are loaded only when first visited.

async function _renderConfigTab(body, agent, panelEl) {
  const mod = await import('../pages/config/page.js');
  if (_expandedAgents.get(agent.id)?.tab !== 'config') return;
  mod.renderAgentConfigDirectory(body, agent, {
    favoriteIds: readAgentPageFavorites(),
    onNavigate: (pageId) => {
      if (!agentPagesCatalog.pages?.some((page) => page.id === pageId)) return;
      _setActive(agent.id, pageId);
      replaceRouteParams({ agent: agent.id, agentView: pageId });
      _renderList();
      _saveViewState();
    },
    onToggleFavorite: (pageId) => {
      const favorites = toggleAgentPageFavorite(pageId);
      const activeTab = _expandedAgents.get(agent.id)?.tab;
      // A page unstarred while active should never become an invisible state.
      if (activeTab === pageId && !favorites.includes(pageId)) _setActive(agent.id, 'model');

      // Rebuild only the existing tab strip. The Config landing, its scroll
      // position, Recent rows, and loaded controls remain mounted.
      const tabBar = panelEl.closest('.agent-row')?.querySelector('.agent-card-tabs')
        || document.querySelector('#agents-detail-region .agents-list-tab-carousel .agent-card-tabs');
      if (tabBar) _populateAgentTabBar(tabBar, agent, panelEl);
      _saveViewState();
      return favorites;
    },
  });
}

async function _renderConfigTopicTab(body, agent, panelEl, topicId) {
  if (!_CONFIG_TOPIC_IDS.has(topicId)) return;
  // Model owns a shared catalog picker whose implementation is versioned below;
  // request this page afresh when that picker changes without invalidating every
  // other configuration topic.
  const pageVersion = topicId === 'model' ? '?v=12' : '';
  const mod = await import(`../pages/${topicId}/page.js${pageVersion}`);
  if (_expandedAgents.get(agent.id)?.tab !== topicId) return;
  mod.renderAgentConfigTopic(body, agent, panelEl, _renderList, {
    onBackToDirectory: () => {
      _setActive(agent.id, 'config');
      _renderList();
      _saveViewState();
    },
  });
}

async function _renderPromptsTab(body, agent, panelEl) {
  const mod = await import('../pages/prompts/page.js');
  mod._renderPromptsTab(body, agent, panelEl);
}

async function _renderTestTab(body, agent) {
  const mod = await import('../pages/agent-loop/page.js');
  mod._renderTestTab(body, agent);
}

async function _renderConnectionsTab(body, agent) {
  const mod = await import('../pages/abilities/page.js?v=784');
  mod._renderConnectionsTab(body, agent);
}

async function _renderMembersTab(body, agent) {
  const mod = await import('../pages/members/page.js');
  mod._renderMembersTab(body, agent);
}

// ── Re-export for external consumers ──────────────────────────────────────────
// These functions need to be available to other modules that import from agents.js



import { _userIsAdmin, _setBinView, _setClonesView } from './state.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';

'use strict';

/**
 * Agents route binding for the shared page chat launcher.
 *
 * Behaviour lives in ui/shared/js/page-chat-launcher.js; this module only
 * supplies what is specific to the Agents route: where the pill mounts, which
 * search control it drives, how the page describes itself, and its handoff tag.
 */

import { mountPageChatLauncher, unmountPageChatLauncher } from './page-chat-launcher.js';

const SPEC = {
  route: 'agents',
  rootId: 'agents-page-chat-launcher',
  hostId: 'tab-agents',
  configKey: 'agents_page_widget',
  storageNamespace: 'agents-page-chat-position',
  sessionTargetName: 'Agents page help',
  handoffKey: 'agents_page_handoff',
  handoffFallback: '[WebAgent Request | Source: Agents Page]: {text}',

  // Inline search fields rendered on the Agents route, in preference order. The
  // route hosts several mutually-exclusive sub-views, so the launcher forwards
  // to whichever one is currently visible:
  //   - the Agents / Abilities tables' own inline fields (below).
  searchSelectors: [
    '[data-agents-search]',
    '.agents-search input',
    '.agent-search input',
    '.ability-search-field .chat-pill-input',
  ],
  // Ability tables intentionally render without their old inline composer and
  // expose the same native filter function on their body element instead.
  controllerSelector: '[data-agents-search-controller]',
  controllerProp: '_agentsSearchController',
  // The Sessions view keeps its real engine input off-screen in a hidden
  // toolbar — staging stateful controls there is what lets the footer drop its
  // own copy of the field. Driving that input reuses the sessions engine's full
  // search: title + content matching with a per-session hit count, the Title/T+C
  // mode toggle and the cross-catalog pass — the launcher never reimplements the
  // filter, it delegates. Reached only once no visible field and no controller
  // resolve, so the sub-view on screen always wins.
  hiddenSearchSelector: '#sessions-search-input',

  // A sub-view can change what the pill IS, not merely what it filters. The
  // Sessions table has no chat: while it is on screen the pill is a search-only
  // control ("Search for a session") whose composer never opens the task widget.
  // Every OTHER Agents sub-view keeps the shared ask-and-search pill. Re-read by
  // the launcher whenever the host flips structure (PAGE-CHAT-LAUNCHER in
  // ui/shared/js/page-chat-launcher.js), so this never needs a remount.
  searchOnly() {
    const section = document.getElementById('sessions-section');
    return !!(section && section.getClientRects().length);
  },

  pageContext() {
    const page = document.getElementById('tab-agents');
    const grid = document.getElementById('agents-grid');
    const activeTab = page?.querySelector('.agents-detail-tab.active');
    const activeRow = page?.querySelector('.agent-row.expanded[data-agent-id]');
    const activeName = activeRow?.querySelector('.agent-card-name')?.textContent?.trim();
    const view = grid?.classList.contains('agents-list-view') ? 'list view'
      : (grid?.classList.contains('carousel') ? 'card carousel view' : 'Agents overview');
    const area = activeTab?.textContent?.trim() || 'Agents overview';
    const agent = activeName || 'All Agents';
    const lines = [
      'Current Agents page context:',
      `- view: ${view}`,
      `- active area: ${area}`,
      `- selected agent: ${agent}`,
    ];
    // When the embedded Sessions view is on screen the launcher is driving its
    // engine input (title + content). Carry that into the handoff message so a
    // question about the list arrives with the same context the search used.
    const sessionsSection = document.getElementById('sessions-section');
    if (sessionsSection && sessionsSection.getClientRects().length) {
      const query = (document.getElementById('sessions-search-input')?.value || '').trim();
      lines.push(`- sessions search: ${query ? `filtering by "${query}" (title + content)` : 'no filter'}`);
    }
    return lines.join('\n');
  },

  fallbacks: {
    aria_label: 'Ask WebAgent or search agents and abilities',
    tooltip: 'Ask WebAgent or search agents and abilities',
    label: 'Ask or Search',
    input_placeholder: 'Ask or search',
    input_aria_label: 'Ask WebAgent or search the current Agents page',
    // Search-only sub-view (the Sessions table). The label doubles as the input
    // placeholder and the aria-label, so every surface reads the same.
    search_label: 'Search for a session',
    search_placeholder: 'Search for a session',
    search_tooltip: 'Searches as you type',
    search_icon: 'search',
  },
};

/** Mount the shared launcher as a non-flow sibling of the Agents page scroller. */
export function mountAgentsPageChatWidget() {
  return mountPageChatLauncher(SPEC);
}

/** Remove the route-scoped launcher. A separately opened task widget remains intact. */
export function unmountAgentsPageChatWidget() {
  unmountPageChatLauncher(SPEC.rootId);
}

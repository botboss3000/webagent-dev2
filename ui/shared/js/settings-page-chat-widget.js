'use strict';

/**
 * Settings route binding for the shared page chat launcher.
 *
 * Replaces the old in-page composer pill (#ac-unified-pa-bar) that used to sit
 * directly in the Settings markup: the Settings area now uses the same floating
 * "Ask or Search" launcher as the Agents page. Typing forwards to the Settings
 * search engine (settings.js → settings-search.js) through the controller the
 * settings module publishes on #app-config-content, so the filtering rules stay
 * in one place. Send opens the standard WebAgent widget.
 *
 * MOUNT POINT — deliberately #main-panel, not #app-config-container. Inside the
 * Instances layout the settings container is re-styled to
 * `display:block; overflow:visible; height:auto` and flows into the Instances
 * scroller, so a bottom-anchored child of it would land at the bottom of the
 * whole settings flow rather than the panel. #main-panel is the panel-sized,
 * position:relative box — the exact analogue of #tab-agents on the Agents route.
 * app3.css gates the launcher's visibility on the Settings host being mounted.
 */

import { mountPageChatLauncher, unmountPageChatLauncher } from './page-chat-launcher.js';

const SPEC = {
  route: 'settings',
  rootId: 'settings-page-chat-launcher',
  hostId: 'main-panel',
  configKey: 'settings_page_widget',
  storageNamespace: 'settings-page-chat-position',
  sessionTargetName: 'Settings help',
  handoffKey: 'settings_page_handoff',
  handoffFallback: '[WebAgent Request | Source: Settings Page]: {text}',

  // The Settings area has no inline search field of its own any more — the
  // launcher's input IS the search box — so the controller is the only target.
  searchSelectors: [],
  controllerSelector: '#app-config-content',
  controllerProp: '_settingsSearchController',

  pageContext() {
    const host = document.getElementById('inst-settings-host');
    const topbar = document.getElementById('settings-topbar');
    const title = document.getElementById('settings-topbar-title')?.textContent?.trim();
    const content = document.getElementById('app-config-content');
    const section = topbar && !topbar.hidden && title ? `Settings → ${title}` : null;
    const searching = content?.classList.contains('ac-searching');
    return [
      'Current Settings page context:',
      `- route: Settings (this device)${host ? '' : ' — not the active view'}`,
      `- view: ${section || 'Settings overview (all sections list)'}`,
      `- search: ${searching ? 'a filter query is active' : 'no filter'}`,
    ].join('\n');
  },

  fallbacks: {
    aria_label: 'Ask WebAgent or search app settings',
    tooltip: 'Ask WebAgent or search app settings',
    label: 'Ask or Search',
    input_placeholder: 'Search settings or ask how to configure something',
    input_aria_label: 'Search app settings or ask WebAgent',
  },
};

/** Mount the Settings launcher as a non-flow sibling of the settings scroller. */
export function mountSettingsPageChatWidget() {
  return mountPageChatLauncher(SPEC);
}

/** Remove the route-scoped launcher. A separately opened task widget remains intact. */
export function unmountSettingsPageChatWidget() {
  unmountPageChatLauncher(SPEC.rootId);
}

'use strict';

import { icon } from '../../../shared/js/icons.js';
import agentPagesCatalog from '../pages/catalog.json' with { type: 'json' };
import { createAgentConfigPage } from './shared.js';

/**
 * Compatibility lifecycle for direct Agent Config pages.
 *
 * Metadata belongs to pages/catalog.json. This module only exposes the existing
 * content-node lifecycle used while each canonical topic page is mounted.
 */
const configDefinitions = (agentPagesCatalog.pages || [])
  .filter((page) => page?.kind === 'config-topic')
  .sort((a, b) => (a.order || 0) - (b.order || 0));

export const AGENT_CONFIG_CATALOG = Object.freeze({
  id: 'config',
  title: agentPagesCatalog.directory?.title || 'Config',
  groups: Object.freeze([...(agentPagesCatalog.directory?.groups || [])]),
  pages: Object.freeze(configDefinitions.map((page) => Object.freeze({
    ...page,
    ...createAgentConfigPage(),
  }))),
});

export const AGENT_CONFIG_PAGES = AGENT_CONFIG_CATALOG.pages;
const PAGE_BY_ID = new Map(AGENT_CONFIG_PAGES.map((page) => [page.id, page]));

export function getAgentConfigPage(id) {
  return PAGE_BY_ID.get(id) || null;
}

/**
 * Shared direct-topic mount controller.
 * The directory itself is rendered by pages/config/directory.js; this controller
 * is only used by a canonical topic page to retain its content node and expose a
 * Back to Config bridge.
 */
export function createAgentConfigPageController(body, { onPageChange = null } = {}) {
  const landing = document.createElement('div');
  landing.className = 'agent-config-landing';
  landing.setAttribute('aria-label', 'Agent configuration topics');

  const pageShell = document.createElement('section');
  pageShell.className = 'agent-config-page-shell';
  pageShell.hidden = true;

  const topbar = document.createElement('div');
  topbar.className = 'agent-config-page-topbar';
  const backButton = document.createElement('button');
  backButton.type = 'button';
  backButton.className = 'agent-config-page-back';
  backButton.title = 'Back to Config';
  backButton.setAttribute('aria-label', 'Back to Config');
  backButton.innerHTML = `${icon('arrow-left', { size: '16px' })}<span>Back to Config</span>`;
  const title = document.createElement('h3');
  title.className = 'agent-config-page-title';
  topbar.append(backButton, title);

  const pageHost = document.createElement('div');
  pageHost.className = 'agent-config-page-host';
  pageShell.append(topbar, pageHost);
  body.append(landing, pageShell);

  const topics = new Map();
  let activeId = '';

  const scrollToTop = () => {
    const scrollRoot = body.closest('.agent-detail-body');
    scrollRoot?.scrollTo({ top: 0, behavior: 'auto' });
  };

  const restoreActiveTopic = () => {
    if (!activeId) return;
    const active = topics.get(activeId);
    if (!active) return;
    active.page.restore(active.section, active.content);
    active.heading.removeAttribute('aria-current');
    activeId = '';
  };

  const close = ({ focusLanding = true } = {}) => {
    const lastActive = topics.get(activeId);
    restoreActiveTopic();
    pageHost.replaceChildren();
    pageShell.hidden = true;
    landing.hidden = false;
    pageShell.removeAttribute('data-config-page');
    title.textContent = '';
    onPageChange?.('');
    scrollToTop();
    if (focusLanding) lastActive?.heading.focus();
  };

  const open = (id) => {
    const page = getAgentConfigPage(id);
    const topic = topics.get(id);
    if (!page || !topic || !topic.section.isConnected) return false;
    if (activeId === id) return true;
    if (activeId) restoreActiveTopic();

    activeId = id;
    topic.page.mount(pageHost, topic.content);
    topic.heading.setAttribute('aria-current', 'page');
    title.textContent = page.title;
    pageShell.dataset.configPage = id;
    landing.hidden = true;
    pageShell.hidden = false;
    onPageChange?.(id);
    scrollToTop();
    backButton.focus();
    return true;
  };

  backButton.addEventListener('click', () => close());

  return {
    landing,
    open,
    close,
    register(id, content) {
      const page = getAgentConfigPage(id);
      const section = content.parentElement;
      const heading = section?.querySelector('.agent-config-topic-heading');
      if (!page || !section || !heading) return false;
      topics.set(id, { page, content, section, heading });
      return true;
    },
    get activePageId() { return activeId; },
  };
}

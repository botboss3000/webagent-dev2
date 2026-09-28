'use strict';

// Mirror of ui/main-panel/agents/pages/config/directory.js for the My Agents
// (All Agents) card. Same DOM/class contract, so the shared Config styling
// applies untouched, and the same favorite semantics — but scoped to the
// my-pages catalog and its own server-backed favorites record.
//
// Deliberate divergence: no "Recent" group. Recents are a per-agent signal
// (a per-agent API/endpoint); the All Agents surface has no single agent, so
// the group is omitted rather than backed by an invented data source.

import { icon } from '../../../../shared/js/icons.js';
import myPagesCatalog from '../catalog.json' with { type: 'json' };
import { isMyAgentsPageFavorite } from '../shared/page-favorites.js';

function sortedPages() {
  return [...(myPagesCatalog.pages || [])]
    .sort((left, right) => (left.order || 0) - (right.order || 0));
}

function sortedGroups() {
  return [...(myPagesCatalog.directory?.groups || [])]
    .sort((left, right) => (left.order || 0) - (right.order || 0));
}

function syncFavoriteButtons(root, favoriteIds) {
  const favorites = new Set(favoriteIds);
  root.querySelectorAll('[data-agent-page-favorite]').forEach((button) => {
    const pageId = button.dataset.agentPageFavorite;
    const favorited = favorites.has(pageId);
    button.setAttribute('aria-pressed', String(favorited));
    const action = pageId === 'config'
      ? 'Config is always in the tab header'
      : (favorited ? 'Remove from tab header' : 'Add to tab header');
    button.setAttribute('aria-label', action);
    button.title = action;
  });
}

function pageRow(page, { onNavigate, onToggleFavorite, favoriteIds }) {
  const row = document.createElement('section');
  row.className = 'agent-config-topic agent-config-topic-row';

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'agent-config-topic-heading';
  button.setAttribute('aria-label', `Open ${page.title}`);
  button.innerHTML =
    `<span class="agent-config-topic-icon" aria-hidden="true">${icon(page.iconName, { size: '18px' })}</span>` +
    `<span class="agent-config-topic-copy"><span class="agent-config-topic-title"></span><span class="agent-config-topic-description"></span></span>`;
  button.querySelector('.agent-config-topic-title').textContent = page.title;
  button.querySelector('.agent-config-topic-description').textContent = page.description;
  button.addEventListener('click', () => onNavigate?.(page.id));

  const favorited = isMyAgentsPageFavorite(page.id, favoriteIds);
  const protectedPage = page.id === 'config';
  const favorite = document.createElement('button');
  favorite.type = 'button';
  favorite.className = 'agent-config-topic-favorite';
  favorite.dataset.agentPageFavorite = page.id;
  favorite.setAttribute('aria-pressed', String(favorited));
  const action = protectedPage ? 'Config is always in the tab header' : (favorited ? 'Remove from tab header' : 'Add to tab header');
  favorite.setAttribute('aria-label', action);
  favorite.title = action;
  favorite.innerHTML = icon('star', { size: '18px' });
  const favoriteIcon = favorite.querySelector('svg');
  if (favorited) favoriteIcon?.classList.add('is-favorited');
  else favoriteIcon?.classList.remove('is-favorited');
  favorite.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    onToggleFavorite?.(page.id);
  });

  row.append(button, favorite);
  return row;
}

/** Render My Agents Config as the JSON-driven My Agents page directory. */
export function renderMyAgentsConfigDirectory(body, {
  onNavigate = null,
  onToggleFavorite = null,
  favoriteIds = [],
} = {}) {
  const landing = document.createElement('div');
  landing.className = 'agent-config-landing';
  // No agent id here: this landing belongs to the global All Agents surface.
  landing.dataset.scope = 'all-agents';
  landing.setAttribute('aria-label', myPagesCatalog.directory?.description || 'My Agents page directory');

  const pages = sortedPages();
  // The directory never lists itself; Config stays reachable from the header.
  const directoryPages = pages.filter((page) => page.id !== 'config');
  const pageById = new Map(pages.map((page) => [page.id, page]));
  const handleToggleFavorite = (pageId) => {
    const next = onToggleFavorite?.(pageId);
    if (Array.isArray(next)) syncFavoriteButtons(landing, next);
    return next;
  };

  const pagesByGroup = new Map();
  for (const page of directoryPages) {
    const groupId = page.directoryGroup || 'workspace';
    if (!pagesByGroup.has(groupId)) pagesByGroup.set(groupId, []);
    pagesByGroup.get(groupId).push(page);
  }

  for (const groupDefinition of sortedGroups()) {
    const groupPages = pagesByGroup.get(groupDefinition.id) || [];
    if (!groupPages.length) continue;
    const group = document.createElement('section');
    group.className = 'agent-config-settings-group';
    group.setAttribute('aria-label', groupDefinition.label);
    const label = document.createElement('div');
    label.className = 'agent-config-settings-group-label';
    label.innerHTML = `${icon(groupDefinition.id === 'workspace' ? 'layout-grid' : 'settings-2', { size: '14px' })}<span></span>`;
    label.querySelector('span').textContent = groupDefinition.label;
    const host = document.createElement('div');
    host.className = 'agent-config-topics';
    host.replaceChildren(...groupPages.map((page) => pageRow(page, { onNavigate, onToggleFavorite: handleToggleFavorite, favoriteIds })));
    group.append(label, host);
    landing.appendChild(group);
  }

  body.replaceChildren(landing);
}

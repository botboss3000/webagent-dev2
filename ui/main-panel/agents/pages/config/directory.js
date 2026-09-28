'use strict';

import { app } from '../../../../shared/js/state.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { icon } from '../../../../shared/js/icons.js';
import agentPagesCatalog from '../catalog.json' with { type: 'json' };
import { isAgentPageFavorite } from '../shared/page-favorites.js';

const RECENT_LIMIT = 3;
const RECENT_CONFIG_PAGE_IDS = new Set(
  (agentPagesCatalog.pages || []).filter((page) => page.kind === 'config-topic').map((page) => page.id),
);

function sortedPages() {
  return [...(agentPagesCatalog.pages || [])]
    .sort((left, right) => (left.order || 0) - (right.order || 0));
}

function sortedGroups() {
  return [...(agentPagesCatalog.directory?.groups || [])]
    .sort((left, right) => (left.order || 0) - (right.order || 0));
}

function recentStorageKey(agent) {
  const userId = app.currentUserId || 'anonymous';
  return `webagent.agent-config.recent.v1:${userId}:${agent.id}`;
}

function readRecentPageIds(agent) {
  try {
    const ids = JSON.parse(localStorage.getItem(recentStorageKey(agent)) || '[]');
    return Array.isArray(ids) ? ids.filter((id) => RECENT_CONFIG_PAGE_IDS.has(id)).slice(0, RECENT_LIMIT) : [];
  } catch (_) {
    return [];
  }
}

function recentTopicsUrl(agent) {
  return `/api/v1/agents/${encodeURIComponent(agent.id)}/config/recent-topics?user_id=${encodeURIComponent(app.currentUserId || '')}`;
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

  const favorited = isAgentPageFavorite(page.id, favoriteIds);
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

function createRecentGroup() {
  const group = document.createElement('section');
  group.className = 'agent-config-settings-group agent-config-recent-group';
  group.setAttribute('aria-label', 'Recent agent configuration');
  const label = document.createElement('div');
  label.className = 'agent-config-settings-group-label';
  label.innerHTML = `${icon('history', { size: '14px' })}<span>Recent</span>`;
  const host = document.createElement('div');
  host.className = 'agent-config-topics';
  group.append(label, host);
  group.hidden = true;
  return { group, host };
}

/** Render Config as the universal, JSON-driven Agents page directory. */
export function renderAgentConfigDirectory(body, agent, {
  onNavigate = null,
  onToggleFavorite = null,
  favoriteIds = [],
} = {}) {
  const landing = document.createElement('div');
  landing.className = 'agent-config-landing';
  landing.dataset.agentId = agent.id;
  landing.setAttribute('aria-label', agentPagesCatalog.directory?.description || 'Agent page directory');

  const pages = sortedPages();
  const directoryPages = pages.filter((page) => page.id !== 'config');
  const pageById = new Map(pages.map((page) => [page.id, page]));
  const handleToggleFavorite = (pageId) => {
    const next = onToggleFavorite?.(pageId);
    if (Array.isArray(next)) syncFavoriteButtons(landing, next);
    return next;
  };
  const recent = createRecentGroup();
  landing.appendChild(recent.group);

  const renderRecent = (ids) => {
    const entries = ids.map((id) => pageById.get(id)).filter(Boolean).slice(0, RECENT_LIMIT);
    recent.group.hidden = !entries.length;
    recent.host.replaceChildren(...entries.map((page) => pageRow(page, { onNavigate, onToggleFavorite: handleToggleFavorite, favoriteIds })));
  };
  renderRecent(readRecentPageIds(agent));

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

  // Keep the previous Recent behavior: cached values paint immediately and the
  // per-user/per-agent API replaces them when available. Direct Config topic
  // pages continue to write this endpoint when opened.
  if (agent.is_mock || !app.currentUserId) return;
  fetch(recentTopicsUrl(agent), { headers: authHeaders() }).then(async (response) => {
    if (!response.ok || !landing.isConnected) return;
    const payload = await response.json();
    if (!Array.isArray(payload.topic_ids)) return;
    renderRecent(payload.topic_ids.filter((id) => RECENT_CONFIG_PAGE_IDS.has(id)).slice(0, RECENT_LIMIT));
  }).catch(() => { /* cached recents remain visible while offline */ });
}

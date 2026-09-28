'use strict';

/**
 * Agents — data loading (fetch agents, profile, app settings).
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {
  _setAgents, _agents,
  _showSystem, _binView, _clonesView,
  _setUserIsAdmin, _setExtendLlmToAgents,
  _expandedAgents, MOCK_AGENT_ID,
} from './state.js';
import {
  ensureAgentCacheHydrated,
  readAgentCache,
  writeAgentCache,
} from './agent-cache.js';
import { reconcileMobileAgentFavorites } from '../pages/shared/agent-favorites.js';
import { hydrateAgentPageFavorites } from '../pages/shared/page-favorites.js';
import { hydrateMyAgentsPageFavorites } from '../my-pages/shared/page-favorites.js';

const CACHE_TTL = {
  agents: 5 * 60 * 1000,
  profile: 15 * 60 * 1000,
  settings: 10 * 60 * 1000,
  counts: 2 * 60 * 1000,
};
const _fresh = new Set();

function _agentListCacheKey() {
  if (_binView) return 'list:bin';
  if (_clonesView) return 'list:clones';
  if (_showSystem) return 'list:system';
  return 'list:main';
}

/** Apply synchronously available IDB/in-memory rows before live fetches. */
export function primeAgentDataFromCache() {
  let changed = false;
  const profile = window.__agentsProfileData || readAgentCache('profile');
  if (!_fresh.has('profile') && profile) {
    _setUserIsAdmin(!!profile.is_admin);
    reconcileMobileAgentFavorites(profile.mobile_agent_favorites);
    changed = true;
  }
  const settings = window.__agentsAppSettingsData || readAgentCache('app-settings');
  if (!_fresh.has('settings') && settings) {
    _setExtendLlmToAgents(settings.extend_llm_to_agents !== false);
    changed = true;
  }
  const shared = (!_showSystem && !_binView && !_clonesView) ? window.__agentsSharedData : null;
  const listData = shared || readAgentCache(_agentListCacheKey());
  if (!_fresh.has('agents') && listData && Array.isArray(listData.agents)) {
    _setAgents(listData.agents);
    changed = true;
  }
  return changed;
}

export function hydrateAgentDataCache() {
  return ensureAgentCacheHydrated().then(primeAgentDataFromCache).catch(() => false);
}

// ── Data loading ──────────────────────────────────────────────────────────────

export async function _loadProfile() {
  // Global Agent-card page favorites are user preferences, not agent data. Start
  // their server hydration with profile load so every card shares one order. The
  // My Agents (All Agents) card keeps a separate record, hydrated the same way.
  const pageFavoritesHydration = hydrateAgentPageFavorites();
  const myAgentsFavoritesHydration = hydrateMyAgentsPageFavorites();
  const cached = window.__agentsProfileData;
  if (cached) {
    _setUserIsAdmin(!!cached.is_admin);
    reconcileMobileAgentFavorites(cached.mobile_agent_favorites);
    writeAgentCache('profile', cached, CACHE_TTL.profile);
    await pageFavoritesHydration;
    await myAgentsFavoritesHydration;
    return;
  }
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      _setUserIsAdmin(!!data.is_admin);
      reconcileMobileAgentFavorites(data.mobile_agent_favorites);
      _fresh.add('profile');
      window.__agentsProfileData = data;
      writeAgentCache('profile', data, CACHE_TTL.profile);
    }
  } catch (e) {
    console.warn('agents: could not load profile', e);
  } finally {
    await pageFavoritesHydration;
    await myAgentsFavoritesHydration;
  }
}

export async function _loadAgents() {
  try {
    const cacheKey = _agentListCacheKey();
    if (!_showSystem && !_binView && !_clonesView) {
      const shared = window.__agentsSharedData;
      if (shared && shared.agents) {
        _setAgents(shared.agents);
        writeAgentCache('list:main', shared, CACHE_TTL.agents);
        return;
      }
    }
    const params = new URLSearchParams({ user_id: app.currentUserId });
    if (_showSystem) params.set('include_system', 'true');
    if (_binView) params.set('view', 'bin');
    else if (_clonesView) params.set('view', 'clones');
    const res = await fetch(`/api/v1/agents?${params.toString()}`, { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      const agentList = data.agents || [];
      _setAgents(agentList);
      _fresh.add('agents');
      writeAgentCache(cacheKey, data, CACHE_TTL.agents);
      if (!_showSystem && !_binView && !_clonesView) window.__agentsSharedData = data;
    }
  } catch (e) {
    console.warn('agents: could not load agent list', e);
  }
}

export async function _loadAppSettings() {
  const cached = window.__agentsAppSettingsData;
  if (cached) {
    _setExtendLlmToAgents(cached.extend_llm_to_agents !== false);
    writeAgentCache('app-settings', cached, CACHE_TTL.settings);
    return;
  }
  try {
    const res = await fetch('/admin/settings/app', { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      _setExtendLlmToAgents(data.extend_llm_to_agents !== false);
      _fresh.add('settings');
      window.__agentsAppSettingsData = data;
      writeAgentCache('app-settings', data, CACHE_TTL.settings);
    }
  } catch (e) {
    // non-fatal — keep default true
  }
}

// The WebAgent template dropdown is fetched directly in tab-config.js (the Config
// tab's Template section); the old header-menu helper `_fetchMockTemplates` that
// lived here was removed with the header template menu.

// ── Fetch connections for abilities/tools counting ────────────────────────────

export async function _fetchAbilitiesAndTools(agent) {
  const countKey = `counts:${agent.id}:abilities`;
  const cachedCount = readAgentCache(countKey);
  if (cachedCount && typeof cachedCount === 'object') {
    // Counts are decorative. Return the last value immediately and refresh it
    // off-path so opening an agent never waits on connections/catalog queries.
    _fetchAbilitiesAndToolsLive(agent, countKey).catch(() => {});
    return cachedCount;
  }
  return _fetchAbilitiesAndToolsLive(agent, countKey);
}

async function _fetchAbilitiesAndToolsLive(agent, countKey) {
  const { _ensureAbilityCatalog, ABILITY_TO_TOOLS, _toolsForAgent } = await import('./state.js');
  await _ensureAbilityCatalog();
  // The Abilities-tab counter must match what the Abilities tab actually shows:
  // the agent's ENABLED abilities, which the tab renders from /connections filtered
  // to `section === 'ability'` (see tab-abilities.js loadConnections). So count the
  // very same set here. The old code instead counted the OAuth ability *registry*
  // (/api/v1/agents/{id}/abilities) — that endpoint only knows the OAuth integration
  // abilities and omits the plugin abilities (git_control, codebase_admin, …) that
  // most agents enable, so the badge read 0 for any agent whose abilities aren't
  // OAuth integrations.
  let connEnabled = new Set();
  try {
    const connRes = await fetch(`/api/v1/agents/${agent.id}/connections?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (connRes.ok) {
      const connData = await connRes.json();
      for (const c of (connData.connections || [])) {
        if (c.enabled && c.section === 'ability') connEnabled.add(c.connection_type);
      }
    }
  } catch (e) { /* non-fatal */ }

  const tools = _toolsForAgent(agent, connEnabled);
  const result = { toolCount: tools.length, abilitiesCount: connEnabled.size };
  writeAgentCache(countKey, result, CACHE_TTL.counts);
  return result;
}

export async function _fetchMembersCount(agent) {
  const countKey = `counts:${agent.id}:members`;
  const cached = readAgentCache(countKey);
  if (Number.isFinite(Number(cached))) {
    _fetchMembersCountLive(agent, countKey).catch(() => {});
    return Number(cached);
  }
  return _fetchMembersCountLive(agent, countKey);
}

async function _fetchMembersCountLive(agent, countKey) {
  const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/members?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
  if (!res.ok) return 0;
  const data = await res.json();
  const admins = data.admins || [];
  const members = data.members || [];
  const count = admins.length + members.length;
  writeAgentCache(countKey, count, CACHE_TTL.counts);
  return count;
}

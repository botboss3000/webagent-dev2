'use strict';

/**
 * Shared membership-level policy helpers for agent configuration tabs.
 * Base-agent settings remain on their existing endpoints; selected levels write
 * only to the profile policy endpoint.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';

const BASE_LEVEL = '__base_agent__';

function _base(agent) {
  return `/api/v1/agents/${encodeURIComponent(agent.id)}`;
}

export function isBaseLevel(slug) {
  return !slug || slug === BASE_LEVEL;
}

export async function loadMemberLevels(agent) {
  const res = await fetch(`${_base(agent)}/profiles`, { headers: authHeaders() });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  const data = await res.json();
  return (data.profiles || []).filter(profile => profile.slug !== 'agent-administrator');
}

export async function saveMemberLevel(agent, profile, policy) {
  const res = await fetch(`${_base(agent)}/profiles/${encodeURIComponent(profile.slug)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      slug: profile.slug,
      name: profile.name || profile.slug,
      description: profile.description || '',
      policy,
      user_id: app.currentUserId,
    }),
  });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  return (await res.json()).profile;
}

export function copyPolicy(profile) {
  return structuredClone((profile && profile.policy) || {});
}

export function policyAllows(values, name) {
  const allowed = Array.isArray(values) ? values : [];
  return allowed.includes('*') || allowed.includes(name);
}

/** A consistent base/level selector with an effective-policy cue. */
export function buildLevelSelector({ levels, selected, onChange, label = 'Configuring', showCue = true }) {
  const wrap = document.createElement('div');
  wrap.className = 'member-policy-selector';
  const labelEl = document.createElement('label');
  labelEl.textContent = label;
  const select = document.createElement('select');
  select.className = 'agents-input member-policy-select';
  select.setAttribute('aria-label', label);

  const base = document.createElement('option');
  base.value = BASE_LEVEL; base.textContent = 'Base agent - admin access';
  select.appendChild(base);
  for (const profile of levels) {
    const option = document.createElement('option');
    option.value = profile.slug; option.textContent = profile.name || profile.slug;
    select.appendChild(option);
  }
  select.value = selected || BASE_LEVEL;
  const cue = document.createElement('div'); cue.className = 'member-policy-cue';
  const selectedProfile = levels.find(profile => profile.slug === select.value);
  cue.textContent = selectedProfile
    ? `Editing ${selectedProfile.name || selectedProfile.slug} policy overlay. Base-agent settings remain unchanged.`
    : 'Editing the agent-wide source configuration; available to the agent admin only';

  select.addEventListener('change', () => onChange(select.value));
  wrap.append(labelEl, select);
  if (showCue) wrap.appendChild(cue);
  return wrap;
}

export function resolvedList(policy, key, available) {
  const values = policy && policy[key];
  return (available || []).filter(name => policyAllows(values, name));
}

export function narrowedList(selected, available) {
  const values = [...new Set(selected || [])];
  const source = [...new Set(available || [])];
  return values.length === source.length ? ['*'] : values;
}

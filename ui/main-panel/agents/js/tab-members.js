'use strict';

/**
 * Agents — Members tab.
 * Shows admin/member access control and activity stats.
 */

import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import {
  _isMockAgent,
} from './state.js';
import { _esc, _timeAgo } from './utils.js';
import { copyPolicy, saveMemberLevel } from './member-level-policy.js';

export async function _renderMembersTab(body, agent) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to manage members.</div>';
    return;
  }
  body.innerHTML = _membersSkeleton();

  let admins = [], members = [], profiles = [], profileMembers = [], userMode = agent.user_mode || 'anonymous';
  try {
    const base = `/api/v1/agents/${encodeURIComponent(agent.id)}`;
    const membersRes = await fetch(`${base}/members?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (!membersRes.ok) { const detail = await membersRes.text(); body.innerHTML = `<div class="members-loading" style="color:var(--danger)">Failed to load members: ${_esc(detail || membersRes.statusText)}</div>`; return; }
    const data = await membersRes.json();
    admins = data.admins || []; members = data.members || [];
    profiles = data.profiles || []; profileMembers = data.profile_members || [];
    // Compatibility with a running server that has not reloaded the enriched
    // Members response yet. These existing agent-admin endpoints provide the
    // same read-only profile data without changing member assignments.
    if (!Array.isArray(data.profiles)) {
      const [profilesRes, profileMembersRes] = await Promise.all([
        fetch(`${base}/profiles`, { headers: authHeaders() }),
        fetch(`${base}/profile-members`, { headers: authHeaders() }),
      ]);
      if (profilesRes.ok) profiles = (await profilesRes.json()).profiles || [];
      if (profileMembersRes.ok) profileMembers = (await profileMembersRes.json()).members || [];
    }
    userMode = data.user_mode || userMode; agent.user_mode = userMode;
  } catch (e) { body.innerHTML = `<div class="members-loading" style="color:var(--danger)">Failed to load members: ${_esc(e.message)}</div>`; return; }

  body.innerHTML = '';
  body.appendChild(_buildAccessPolicyControl(agent, userMode, body));

  const notice = document.createElement('div'); notice.className = 'members-notice';
  notice.textContent = 'Activity counts reflect this agent only.';
  body.appendChild(notice);

  body.appendChild(_buildMembersSection(agent, 'Admins', admins, 'admin', body));
  // This standalone sibling intentionally sits outside both tables: after the
  // administrator section and before the ordinary-members section.
  body.appendChild(_buildMemberLevelsSection(agent, profiles, profileMembers, body));
  body.appendChild(_buildMembersSection(agent, 'Members', members, 'member', body));
}

function _buildAccessPolicyControl(agent, currentMode, panelBody) {
  const wrap = document.createElement('div'); wrap.className = 'members-policy';
  const opts = [
    ['anonymous', 'Anonymous', 'Anyone with the link can chat. No registration needed.'],
    ['register', 'Registered', 'Users must have a registered account to chat.'],
    ['authorized', 'Authorized', 'Registered users must be authorized by an admin before they can chat.'],
  ];
  const title = document.createElement('div'); title.className = 'members-policy-title';
  title.textContent = 'Access policy';
  wrap.appendChild(title);
  const choices = document.createElement('div'); choices.className = 'members-policy-choices';
  for (const [val, label, hint] of opts) {
    const id = `acp-${agent.id}-${val}`;
    const optEl = document.createElement('label'); optEl.className = 'members-policy-opt' + (currentMode === val ? ' active' : '');
    optEl.htmlFor = id;
    optEl.innerHTML = `<input type="radio" id="${_esc(id)}" name="acp-${_esc(agent.id)}" value="${_esc(val)}" ${currentMode === val ? 'checked' : ''}>
      <div class="members-policy-opt-body">
        <div class="members-policy-opt-label">${_esc(label)}</div>
        <div class="members-policy-opt-hint">${_esc(hint)}</div>
      </div>`;
    optEl.querySelector('input').addEventListener('change', async (ev) => {
      const newMode = ev.target.value;
      try {
        const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/user-mode`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ user_id: app.currentUserId, user_mode: newMode }),
        });
        if (!res.ok) throw new Error(await res.text());
        agent.user_mode = newMode; _renderMembersTab(panelBody, agent);
      } catch (e) { alert('Failed to update access policy: ' + e.message); _renderMembersTab(panelBody, agent); }
    });
    choices.appendChild(optEl);
  }
  wrap.appendChild(choices); return wrap;
}

function _buildMemberLevelsSection(agent, profiles, profileMembers, panelBody) {
  const section = document.createElement('details');
  section.className = 'members-levels';
  section.dataset.memberLevels = 'true';
  const countBySlug = new Map();
  for (const member of profileMembers) {
    const slug = member?.profile?.slug;
    if (slug) countBySlug.set(slug, (countBySlug.get(slug) || 0) + 1);
  }
  const ordinaryProfiles = profiles.filter((profile) => profile.slug !== 'agent-administrator');
  const summary = document.createElement('summary');
  summary.innerHTML = `Member Levels &amp; Policies <span class="members-levels-count">${ordinaryProfiles.length}</span>`;
  section.appendChild(summary);
  const content = document.createElement('div'); content.className = 'members-levels-body';
  if (!ordinaryProfiles.length) {
    content.innerHTML = '<div class="members-empty">No membership levels are available for this agent.</div>';
  } else {
    for (const profile of ordinaryProfiles) {
      const policy = profile.policy || {};
      const limits = policy.limits || {};
      const level = document.createElement('article'); level.className = 'members-level-card';
      const name = document.createElement('div'); name.className = 'members-level-name'; name.textContent = profile.name || profile.slug;
      const description = document.createElement('div'); description.className = 'members-level-description'; description.textContent = profile.description || 'No description provided.';
      const meta = document.createElement('div'); meta.className = 'members-level-meta';
      const count = document.createElement('span'); count.textContent = `${countBySlug.get(profile.slug) || 0} member${(countBySlug.get(profile.slug) || 0) === 1 ? '' : 's'}`; meta.appendChild(count);
      const addLimitChip = (key, label, value) => {
        const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'members-level-limit-chip';
        chip.textContent = value ? `${value} ${label}` : `No ${label}`;
        chip.title = `Edit ${label} for ${profile.name || profile.slug}`;
        chip.addEventListener('click', () => {
          const existing = level.querySelector('.members-level-limit-editor');
          if (existing) { existing.remove(); return; }
          level.appendChild(_buildLimitEditor(agent, profile, key, label, panelBody));
        });
        meta.appendChild(chip);
      };
      addLimitChip('daily_turns', 'daily turns', limits.daily_turns);
      addLimitChip('monthly_turns', 'monthly turns', limits.monthly_turns);
      level.append(name, description, meta); content.appendChild(level);
    }
  }
  section.appendChild(content);
  return section;
}

function _buildLimitEditor(agent, profile, key, label, panelBody) {
  const editor = document.createElement('form'); editor.className = 'members-level-limit-editor';
  const policy = copyPolicy(profile); const limits = { ...(policy.limits || {}) };
  const caption = document.createElement('label'); caption.textContent = `Set ${label}`;
  const input = document.createElement('input'); input.type = 'number'; input.min = '1'; input.step = '1'; input.className = 'agents-input'; input.value = limits[key] || '';
  input.placeholder = 'Unlimited'; caption.appendChild(input);
  const inherit = document.createElement('label'); inherit.className = 'members-level-limit-inherit';
  const check = document.createElement('input'); check.type = 'checkbox'; check.checked = !limits[key];
  inherit.append(check, document.createTextNode(' No limit'));
  check.addEventListener('change', () => { input.disabled = check.checked; if (check.checked) input.value = ''; }); input.disabled = check.checked;
  const save = document.createElement('button'); save.type = 'submit'; save.className = 'members-btn authorize'; save.textContent = 'Save';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'members-btn restrict'; cancel.textContent = 'Cancel';
  const status = document.createElement('span'); status.className = 'member-policy-save-status';
  cancel.addEventListener('click', () => editor.remove());
  editor.addEventListener('submit', async (event) => {
    event.preventDefault(); const value = Number(input.value);
    if (!check.checked && (!Number.isInteger(value) || value < 1)) { status.textContent = 'Enter a whole number of at least 1.'; return; }
    save.disabled = cancel.disabled = true; status.textContent = 'Saving…';
    try {
      if (check.checked) delete limits[key]; else limits[key] = value;
      policy.limits = limits;
      await saveMemberLevel(agent, profile, policy);
      _renderMembersTab(panelBody, agent);
    } catch (error) { status.textContent = `Save failed: ${error.message}`; save.disabled = cancel.disabled = false; }
  });
  editor.append(caption, inherit, save, cancel, status); return editor;
}

function _buildMembersSection(agent, title, rows, kind, panelBody) {
  const sec = document.createElement('div'); sec.className = 'members-section';
  const header = document.createElement('div'); header.className = 'members-section-header';
  header.innerHTML = `<span class="members-section-title">${_esc(title)}</span><span class="members-section-count">${rows.length}</span>`;
  sec.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'members-empty';
    empty.textContent = kind === 'admin' ? 'No admins assigned to this agent yet.' : 'No members have used this agent yet.';
    sec.appendChild(empty); return sec;
  }
  const showActions = kind === 'member';
  const wrap = document.createElement('div'); wrap.className = 'members-table-wrap';
  const table = document.createElement('table'); table.className = 'members-table';
  table.innerHTML = `<thead><tr><th>User</th><th>Member level</th><th>Channel</th><th class="members-num">Sessions</th><th class="members-num">Messages</th><th>Last login</th>${showActions ? '<th>Status</th><th></th>' : ''}</tr></thead><tbody></tbody>`;
  const tbody = table.querySelector('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || r.username || r.user_id;
    const subId = r.username && r.username !== name ? r.username : r.user_id;
    const channel = r.channel || (r.username ? 'web' : '\u2014');
    const level = r.member_level || { name: 'Member', assigned: false };
    const levelHtml = `<td><span class="members-level-badge" title="${_esc(level.assigned ? 'Assigned membership level' : 'Default member level')}">${_esc(level.name || 'Member')}</span></td>`;
    const last = r.last_login_at ? _timeAgo(r.last_login_at) : '\u2014';
    let statusHtml = '', actionHtml = '';
    if (showActions) {
      const isAuth = !!r.is_authorized;
      statusHtml = `<td><span class="members-status ${isAuth ? 'ok' : 'pending'}">${isAuth ? 'Authorized' : 'Pending'}</span></td>`;
      actionHtml = `<td class="members-actions"><button class="members-btn ${isAuth ? 'restrict' : 'authorize'}" data-act="${isAuth ? 'restrict' : 'authorize'}" data-uid="${_esc(r.user_id)}">${isAuth ? 'Restrict' : 'Authorize'}</button></td>`;
    }
    tr.innerHTML = `<td><div class="members-user-name">${_esc(name)}</div><div class="members-user-sub">${_esc(subId)}</div></td>${levelHtml}<td>${_esc(channel)}</td><td class="members-num">${r.session_count ?? 0}</td><td class="members-num">${r.interaction_count ?? 0}</td><td>${_esc(last)}</td>${statusHtml}${actionHtml}`;
    tbody.appendChild(tr);
  }
  if (showActions) {
    tbody.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button.members-btn');
      if (!btn) return;
      const uid = btn.dataset.uid; const act = btn.dataset.act; btn.disabled = true;
      try {
        const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/members/${encodeURIComponent(uid)}/${act}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ user_id: app.currentUserId }),
        });
        if (!res.ok) throw new Error(await res.text());
        _renderMembersTab(panelBody, agent);
      } catch (e) { alert('Action failed: ' + e.message); btn.disabled = false; }
    });
  }
  wrap.appendChild(table); sec.appendChild(wrap); return sec;
}

// Loading skeleton — echoes the access-policy card + two member sections so the
// tab has structure while the /members fetch resolves (shared .sk-shimmer in app3.css).
function _membersSkeleton() {
  const dot = '<span class="mem-sk sk-shimmer" style="width:14px;height:14px;border-radius:50%;flex:none;"></span>';
  const opt = (w) => `<div class="mem-sk-opt">${dot}<span class="mem-sk sk-shimmer" style="width:${w};"></span></div>`;
  const row = (w) => `<div class="mem-sk-row"><span class="mem-sk sk-shimmer" style="width:${w};"></span></div>`;
  const section = (titleW, rows) => `<div class="mem-sk-section">
      <span class="mem-sk sk-shimmer mem-sk-head" style="width:${titleW};"></span>
      ${rows.map(row).join('')}
    </div>`;
  return `<div class="members-skeleton" aria-hidden="true">
    <div class="mem-sk-policy">
      <span class="mem-sk sk-shimmer mem-sk-head" style="width:90px;"></span>
      ${opt('62%')}${opt('54%')}${opt('68%')}
    </div>
    ${section('64px', ['78%', '66%'])}
    ${section('82px', ['84%', '70%', '58%'])}
  </div>`;
}
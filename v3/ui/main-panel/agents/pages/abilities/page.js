// V3 data adapter for the v2 Abilities composition, prompts group, and ability rows.
import { buildAbilitiesOverview } from './overview.js';
import { buildAdministratorControls } from './administrator.js';
import { buildPromptsGroup } from './prompts.js';
import { buildGroup, setGroupStatus } from './table.js';
import { buildPolicyControls, permissionControl, effectiveLabel, readinessLabel } from './policy.js';

import { _buildMoreButton, _buildToolRow, _buildAbilitySettingsList, closeFloatingMenu } from '../../../../shared/ability-row-controls.js';
import { buildSkillRow } from './rows.js';

const node = (tag, className = '', text = '') => {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
};
const chevronSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>';
const modes = ['off', 'discoverable', 'visible'];
let searchText = '';
let expandedAbility = null;

// The selected tenant and server authority govern every read and save on this page.
export async function renderAbilities(host, request, options) {
  const { agentId, levelId = '', previewUser = '' } = options;
  const scope = '/agents/' + encodeURIComponent(agentId);
  const generation = (host._abilityGeneration || 0) + 1;
  host._abilityGeneration = generation;
  const data = await request(scope + '/abilities');
  const canManage = data.authority?.can_manage === true;
  const [slotsResult, compositionResult, effective, repository] = await Promise.all([
    canManage ? request(scope + '/slots').catch(() => ({ slots: [] })) : { slots: [] },
    canManage ? request(scope + '/context-composition').catch(() => ({ tokens: 0 })) : { tokens: 0 },
    (levelId || previewUser) && canManage
      ? request(scope + '/abilities/effective?' + new URLSearchParams({
          ...(levelId ? { level_id: levelId } : {}),
          ...(previewUser ? { user_id: previewUser } : {}),
        })) : data,
    canManage ? request(scope + '/repository-connections').catch(() => ({ load_failed: true })) : null,
  ]);
  if (host._abilityGeneration !== generation) return;
  const effectiveAbilities = effective.abilities || [];
  const projection = ability => effectiveAbilities.find(item => item.id === ability.id) || ability;
  const rerender = next => renderAbilities(host, request, { ...options, ...next });
  const abilities = data.abilities || [];
  const slots = slotsResult.slots || [];
  const groups = new Map();
  for (const ability of abilities) {
    const key = ability.group || 'Other';
    if (!groups.has(key)) groups.set(key, { name: ability.group_name || key, items: [] });
    groups.get(key).items.push(ability);
  }
  const page = node('div', 'conn-tab-content v3-abilities-page');
  const overview = buildAbilitiesOverview();
  overview.update(compositionResult, abilities, slots);
  const status = node('p', 'v3-ability-status');
  status.setAttribute('role', 'status');
  status.textContent = options.administratorNotice || '';
  const search = node('input', 'ac-input v3-ability-search');
  search.type = 'search';
  search.placeholder = 'Search abilities, skills, tools and prompts';
  search.setAttribute('aria-label', 'Search abilities');
  search.value = searchText;
  const list = node('div', 'conn-grid ac-list v3-abilities-list');
  const prompts = canManage ? await buildPromptsGroup(request, agentId) : null;
  if (prompts) list.append(prompts);
  const controls = await buildPolicyControls({ data, request, scope, levelId, previewUser, rerender, status });
  if (host._abilityGeneration !== generation) return;
  overview.querySelector('[data-tier-selector]').replaceChildren(controls.selector);

  const save = async (ability, kind, mode, control, toolName = null) => {
    control.disabled = true;
    try {
      const path = scope + '/abilities/' + encodeURIComponent(ability.id) +
        (kind === 'tool' ? '/tools/' + encodeURIComponent(toolName) + '/mode' :
          kind === 'skill' && toolName && toolName !== 'core'
            ? '/skills/' + encodeURIComponent(toolName) + '/visibility'
            : '/' + (kind === 'skill' ? 'skill-visibility' : 'visibility'));
      await request(path, 'PUT', { visibility: mode, expected_revision: data.revision });
      expandedAbility = ability.id;
      await rerender({});
    } catch (error) {
      status.textContent = error.status === 409 ? 'Settings changed. Reload before saving: ' + error.message : error.message;
      control.disabled = false;
      throw error;
    }
  };
  const modeMenu = (ability, kind, current, toolName = null, anchorOnly = false) => {
    const trigger = _buildMoreButton(() => modes.map(mode => ({
      icon: mode === 'off' ? 'eye-off' : mode === 'visible' ? 'eye' : 'download',
      label: mode[0].toUpperCase() + mode.slice(1), checked: current === mode,
      disabled: !canManage || !!levelId || !!previewUser,
      action: () => save(ability, kind, mode, trigger, toolName).catch(() => {}),
    })), { title: 'Choose ' + kind + ' availability' });
    if (anchorOnly) trigger.classList.add('ac-ability-more-anchor');
    return trigger;
  };
  const openToolSkill = async (ability, tool) => {
    const dialog = node('dialog', 'v3-tool-skill-dialog');
    const heading = node('h3', '', tool + '.skill.md');
    const pre = node('pre', '', 'Loading tool skill…');
    const close = node('button', 'ac-btn ac-btn-sm', 'Close');
    close.type = 'button';
    close.addEventListener('click', () => dialog.close());
    dialog.append(heading, pre, close);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    document.body.append(dialog);
    dialog.showModal();
    try {
      const result = await request(scope + '/abilities/' + encodeURIComponent(ability.id) +
        '/tools/' + encodeURIComponent(tool) + '/skill-content');
      if (dialog.isConnected) pre.textContent = result.content || 'No skill text.';
    } catch (error) {
      if (dialog.isConnected) pre.textContent = error.message || 'Could not load tool skill.';
    }
  };
  const buildAbilityBody = (detail, ability) => {
    if (detail.childElementCount) return;
    const tools = _buildAbilitySettingsList();
    tools.classList.add('v3-ability-tools');
    const generalSkills = ability.general_skills ||
      (ability.has_skill ? [{ skill_id: 'core', title: 'Skill',
        when_to_use: ability.skill_summary || '', mode: ability.skill_visibility || 'visible' }] : []);
    for (const entry of generalSkills) {
      const skill = buildSkillRow(entry, {
        canEdit: canManage && !levelId && !previewUser,
        menu: modeMenu(ability, 'skill', entry.mode || 'visible', entry.skill_id, true),
        loadContent: () => request(scope + '/abilities/' + encodeURIComponent(ability.id) +
          (entry.skill_id === 'core' ? '/skill-content' :
            '/skills/' + encodeURIComponent(entry.skill_id) + '/content')),
      });
      tools.append(skill.row, skill.body);
    }
    const administrator = buildAdministratorControls({ ability, data, repository, request, scope,
      levelId, previewUser, status, rerender });
    if (administrator) {
      for (const setting of administrator.querySelectorAll(':scope > .ac-ability-row')) tools.append(setting);
    }
    for (const tool of ability.tools || []) {
      const effectiveTool = (projection(ability).tools || []).find(item => item.name === tool.name) || tool;
      const saveToolPermission = async mode => {
        const level = data.capability_levels?.levels?.[levelId];
        const policy = structuredClone(level || data.permissions || {});
        policy.tool_permissions ||= {};
        policy.tool_permissions[tool.name] = mode;
        try {
          if (level) {
            await request(scope + '/capability-levels/' + encodeURIComponent(levelId), 'PUT',
              { expected_revision: data.capability_levels.revision, level: policy });
          } else {
            await request(scope + '/ability-permissions', 'PUT', {
              expected_revision: data.permissions.revision,
              ability_permissions: policy.ability_permissions || {},
              tool_permissions: policy.tool_permissions,
            });
          }
          expandedAbility = ability.id;
          await rerender({ administratorNotice: 'Tool permission saved.' });
        } catch (error) {
          status.textContent = error.status === 409 ? 'Settings changed. Reload before saving: ' + error.message : error.message;
          throw error;
        }
      };
      const toolRow = _buildToolRow({
        ...tool,
        description: tool.description || '',
        callCount: tool.call_count,
        // The control edits the agent's configured layer. Effective access can
        // still be Deny because of the account tier, visibility or port status.
        permMode: data.permissions?.tool_permissions?.[tool.name] ||
          data.permissions?.tool_permissions?.['*'] || 'auto',
      }, {
        canEdit: canManage && !previewUser && !levelId,
        showPerm: true,
        abilityId: ability.id,
        hasToolSkill: tool.has_tool_skill === true,
        onOpenToolSkill: name => openToolSkill(ability, name),
        onPermissionChange: (_, mode) => saveToolPermission(mode),
        onVisibilityChange: (_, mode) => save(ability, 'tool', mode, toolRow.querySelector('.ac-tri'), tool.name),
      });
      toolRow.title = [tool.supported ? '' : 'Unavailable in v3', effectiveLabel(effectiveTool)].filter(Boolean).join(' · ');
      tools.append(toolRow);
    }
    detail.append(tools);
    // V3-specific access layering stays available outside the reference row title.
    if (canManage) {
      const access = node('details', 'v3-ability-access');
      access.append(node('summary', 'ac-hint', 'V3 access policy'));
      access.append(permissionControl({ data, request, scope, levelId, previewUser,
        kind: 'ability', id: ability.id, status, rerender }));
      access.append(node('p', 'ac-hint', [readinessLabel(ability), effectiveLabel(projection(ability))].join(' · ')));
      detail.append(access);
    }
    if (administrator) detail.append(administrator);
    if (ability.migration_note) detail.append(node('p', 'v3-ability-note', ability.migration_note));
  };
  for (const [groupId, group] of groups) {
    const groupRow = buildGroup(groupId, group.name, 'shield', 'var(--brand)', '');
    setGroupStatus(groupRow, group.items);
    const body = groupRow.querySelector('.ac-group-body');
    for (const ability of group.items) {
      const row = node('div', 'conn-card ac-row v3-ability' +
        (ability.visibility !== 'off' ? ' enabled' : ''));
      row.dataset.abilityId = ability.id;
      row.dataset.exposure = ability.visibility;
      const header = node('div', 'conn-card-header ac-ability-row conn-card-header-clickable');
      const chevron = node('span', 'ac-row-chevron ac-row-chevron--left');
      chevron.innerHTML = chevronSvg;
      const icon = node('span', 'conn-icon ac-ability-icon');
      const iconGlyph = node('i');
      iconGlyph.setAttribute('data-lucide', ability.icon || 'box');
      iconGlyph.style.cssText = 'width:16px;height:16px';
      icon.append(iconGlyph);
      if (ability.color) icon.style.color = ability.color;
      const label = node('div', 'ac-ability-label');
      label.append(node('div', 'conn-name ac-ability-name', ability.name),
        node('div', 'ac-ability-desc', ability.description));
      const projected = projection(ability);
      const availability = node('span', 'ac-ability-status',
        [readinessLabel(ability), effectiveLabel(projected)].filter(Boolean).join(' · '));
      availability.title = ['Declared: ' + !!ability.declared, 'Installed: ' + !!ability.installed,
        'Configured: ' + !!ability.configured, readinessLabel(ability)].join(' · ');
      const toggleWrap = node('label', 'conn-toggle-wrap ac-ability-toggle-wrap');
      const toggle = node('input', 'conn-toggle');
      toggle.type = 'checkbox';
      toggle.disabled = !canManage || !!levelId || !!previewUser;
      toggle.checked = ability.visibility !== 'off';
      toggle.setAttribute('aria-label',
        ability.name + ' availability: ' + ability.visibility + '. Toggle on or off.');
      const menu = modeMenu(ability, 'ability', ability.visibility, null, true);
      toggle.addEventListener('click', event => {
        event.stopPropagation();
      });
      // The switch saves immediately; the adjacent menu offers Discoverable.
      toggle.addEventListener('change', () => {
        const mode = toggle.checked ? 'visible' : 'off';
        save(ability, 'ability', mode, toggle).catch(() => {
          toggle.checked = ability.visibility !== 'off';
        });
      });
      toggleWrap.append(toggle, node('span', 'conn-toggle-track'));
      header.append(chevron, icon, label, toggleWrap, menu);
      header.title = availability.title + ' · ' + availability.textContent;
      const detail = node('div', 'ac-ability-body');
      const open = () => {
        const expanded = row.classList.toggle('expanded');
        row.classList.toggle('conn-card-expanded', expanded);
        header.setAttribute('aria-expanded', String(expanded));
        if (expanded) buildAbilityBody(detail, ability);
      };
      header.tabIndex = 0;
      header.setAttribute('role', 'button');
      header.setAttribute('aria-expanded', 'false');
      header.addEventListener('click', event => {
        if (event.target.closest('button, label, input, .v3-mode-menu')) return;
        open();
      });
      header.addEventListener('keydown', event => {
        if (event.target.closest('button, label, input, select, .v3-mode-menu')) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          open();
        }
      });
      row.append(header, detail);
      body.append(row);
      if (expandedAbility === ability.id) {
        groupRow.classList.add('expanded');
        groupRow.querySelector('.ac-group-head').setAttribute('aria-expanded', 'true');
        open();
      }
    }
    list.append(groupRow);
  }
  const filter = () => {
    searchText = search.value.trim().toLowerCase();
    for (const row of list.querySelectorAll('.v3-ability')) {
      const ability = abilities.find(item => item.id === row.dataset.abilityId);
      row.hidden = !!searchText && ![ability.name, ability.description, ability.skill_summary,
        ...(ability.tools || []).map(tool => tool.name)]
        .some(value => String(value || '').toLowerCase().includes(searchText));
    }
    for (const group of list.querySelectorAll('.ac-group:not(.ac-group-prompts)')) {
      group.hidden = !group.querySelector('.v3-ability:not([hidden])');
      if (searchText && !group.hidden) {
        group.classList.add('expanded');
        group.querySelector('.ac-group-head').setAttribute('aria-expanded', 'true');
      }
    }
    if (prompts) prompts.hidden = !!searchText && !slots.some(slot =>
      [slot.slot_name, slot.content].some(value =>
        String(value || '').toLowerCase().includes(searchText)));
  };
  search.addEventListener('input', filter);
  page.append(overview, controls.panel, search, list, status);
  const reload = node('button', 'ac-btn ac-btn-sm', 'Reload abilities');
  reload.type = 'button';
  reload.onclick = () => rerender({ administratorNotice: '' }).catch(error => { status.textContent = error.message; });
  page.append(reload);
  closeFloatingMenu();
  host.replaceChildren(page);
  window.lucide?.createIcons({ nodes: [...page.querySelectorAll('[data-lucide]:not(.lucide)')] });
  filter();
}

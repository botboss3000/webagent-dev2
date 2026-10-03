// V3 data adapter for the v2 Abilities composition, prompts group, and ability rows.
import { buildAbilitiesOverview } from './overview.js';
import { buildAdministratorControls } from './administrator.js';
import { buildPromptsGroup } from './prompts.js';
import { buildGroup, setGroupStatus } from './table.js';
import { buildPolicyControls, permissionControl, effectiveLabel, readinessLabel } from './policy.js';

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
          '/' + (kind === 'skill' ? 'skill-visibility' : 'visibility'));
      await request(path, 'PUT', { visibility: mode, expected_revision: data.revision });
      expandedAbility = ability.id;
      await rerender({});
    } catch (error) {
      status.textContent = error.status === 409 ? 'Settings changed. Reload before saving: ' + error.message : error.message;
      control.disabled = false;
    }
  };
  const modeMenu = (ability, kind, current, toolName = null, anchorOnly = false) => {
    const holder = node('div', 'v3-mode-holder');
    const trigger = node('button', 'ac-btn ac-btn-sm v3-mode-trigger' +
      (anchorOnly ? ' ac-ability-more-anchor' : ''), '⋯');
    trigger.type = 'button';
    trigger.disabled = !canManage || !!levelId || !!previewUser;
    trigger.title = (kind === 'skill' ? 'Skill' : kind === 'tool' ? 'Tool' : 'Ability') + ' options';
    trigger.setAttribute('aria-label', trigger.title);
    trigger.setAttribute('aria-expanded', 'false');
    const menu = node('div', 'v3-mode-menu');
    menu.hidden = true;
    menu.append(node('div', 'v3-menu-caption',
      kind === 'skill' ? 'Skill visibility' : kind === 'tool' ? 'Tool mode' : 'Ability visibility'));
    for (const mode of modes) {
      const choice = node('button', 'v3-mode-choice', mode[0].toUpperCase() + mode.slice(1));
      choice.type = 'button';
      choice.setAttribute('aria-pressed', String(current === mode));
      choice.addEventListener('click', event => {
        event.stopPropagation();
        void save(ability, kind, mode, choice, toolName);
      });
      menu.append(choice);
    }
    trigger.addEventListener('click', event => {
      event.stopPropagation();
      const opening = menu.hidden;
      page.querySelectorAll('.v3-mode-menu').forEach(item => { item.hidden = true; });
      page.querySelectorAll('.v3-mode-trigger').forEach(item => item.setAttribute('aria-expanded', 'false'));
      menu.hidden = !opening;
      trigger.setAttribute('aria-expanded', String(opening));
    });
    holder.append(trigger, menu);
    return holder;
  };
  const buildAbilityBody = (detail, ability) => {
    if (detail.childElementCount) return;
    detail.append(node('p', 'ac-hint', ['Declared: ' + !!ability.declared, 'Installed: ' + !!ability.installed,
      'Configured: ' + !!ability.configured, readinessLabel(ability)].join(' · ')));
    if (ability.has_skill) {
      const skill = node('div', 'ac-ability-row ac-row-static v3-ability-skill');
      const label = node('div', 'ac-ability-label');
      label.append(node('div', 'ac-ability-name', 'Skill'),
        node('div', 'ac-ability-desc', ability.skill_summary || 'Ability instructions'));
      skill.append(label, node('span', 'v3-mode-badge', ability.skill_visibility || 'visible'),
        modeMenu(ability, 'skill', ability.skill_visibility || 'visible'));
      detail.append(skill);
    }
    const tools = node('div', 'ac-list v3-ability-tools');
    for (const tool of ability.tools || []) {
      const effectiveTool = (projection(ability).tools || []).find(item => item.name === tool.name) || tool;
      const toolRow = node('div', 'ac-ability-row ac-row-static');
      const label = node('div', 'ac-ability-label');
      label.append(node('div', 'ac-ability-name', tool.name),
        node('div', 'ac-ability-desc', tool.supported ?
          String(tool.policy).replaceAll('_', ' ') : 'Unavailable in v3'));
      toolRow.append(label, node('span', 'v3-mode-badge', tool.mode || 'visible'),
        modeMenu(ability, 'tool', tool.mode || 'visible', tool.name));
      toolRow.append(permissionControl({ data, request, scope, levelId, previewUser, kind: 'tool', id: tool.name, status, rerender }),
        node('span', 'ac-hint', effectiveLabel(effectiveTool)));
      tools.append(toolRow);
    }
    detail.append(tools);
    const administrator = buildAdministratorControls({ ability, data, repository, request, scope,
      levelId, previewUser, status, rerender });
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
        ability.name + ' availability: ' + ability.visibility + '. Choose availability.');
      const menu = modeMenu(ability, 'ability', ability.visibility, null, true);
      toggle.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        menu.querySelector('.v3-mode-trigger').click();
      });
      toggleWrap.append(toggle, node('span', 'conn-toggle-track'));
      header.append(chevron, icon, label, availability, toggleWrap, menu,
        permissionControl({ data, request, scope, levelId, previewUser, kind: 'ability', id: ability.id, status, rerender }));
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
  page.addEventListener('click', event => {
    if (!event.target.closest('.v3-mode-holder, .conn-toggle-wrap')) {
      page.querySelectorAll('.v3-mode-menu').forEach(item => { item.hidden = true; });
    }
  });
  page.append(overview, controls.panel, search, list, status);
  const reload = node('button', 'ac-btn ac-btn-sm', 'Reload abilities');
  reload.type = 'button';
  reload.onclick = () => rerender({ administratorNotice: '' }).catch(error => { status.textContent = error.message; });
  page.append(reload);
  host.replaceChildren(page);
  window.lucide?.createIcons({ nodes: [...page.querySelectorAll('[data-lucide]:not(.lucide)')] });
  filter();
}

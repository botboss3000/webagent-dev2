// V3 data adapter for the v2 Abilities composition, prompts group, and ability rows.
import { buildAbilitiesOverview } from './overview.js';
import { buildPromptsGroup } from './prompts.js';
import { buildGroup, setGroupStatus } from './table.js';

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

export async function renderAbilities(host, request) {
  const [data, slotsResult, compositionResult] = await Promise.all([
    request('/agents/shared_default/abilities'),
    request('/agents/shared_default/slots').catch(() => ({ slots: [] })),
    request('/agents/shared_default/context-composition').catch(() => ({ tokens: 0 })),
  ]);
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
  const search = node('input', 'ac-input v3-ability-search');
  search.type = 'search';
  search.placeholder = 'Search abilities, skills, tools and prompts';
  search.setAttribute('aria-label', 'Search abilities');
  search.value = searchText;
  const list = node('div', 'conn-grid ac-list v3-abilities-list');
  const prompts = await buildPromptsGroup(request);
  list.append(prompts);

  const save = async (ability, kind, mode, control, toolName = null) => {
    control.disabled = true;
    try {
      const path = '/agents/shared_default/abilities/' + encodeURIComponent(ability.id) +
        (kind === 'tool' ? '/tools/' + encodeURIComponent(toolName) + '/mode' :
          '/' + (kind === 'skill' ? 'skill-visibility' : 'visibility'));
      await request(path, 'PUT', { visibility: mode, expected_revision: data.revision });
      expandedAbility = ability.id;
      await renderAbilities(host, request);
    } catch (error) {
      status.textContent = error.message;
      control.disabled = false;
    }
  };
  const modeMenu = (ability, kind, current, toolName = null, anchorOnly = false) => {
    const holder = node('div', 'v3-mode-holder');
    const trigger = node('button', 'ac-btn ac-btn-sm v3-mode-trigger' +
      (anchorOnly ? ' ac-ability-more-anchor' : ''), '⋯');
    trigger.type = 'button';
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
      const toolRow = node('div', 'ac-ability-row ac-row-static');
      const label = node('div', 'ac-ability-label');
      label.append(node('div', 'ac-ability-name', tool.name),
        node('div', 'ac-ability-desc', tool.supported ?
          String(tool.policy).replaceAll('_', ' ') : 'Unavailable in v3'));
      toolRow.append(label, node('span', 'v3-mode-badge', tool.mode || 'visible'),
        modeMenu(ability, 'tool', tool.mode || 'visible', tool.name));
      tools.append(toolRow);
    }
    detail.append(tools);
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
      const availability = node('span', 'ac-ability-status',
        ability.available ? '' : 'Pending port');
      const toggleWrap = node('label', 'conn-toggle-wrap ac-ability-toggle-wrap');
      const toggle = node('input', 'conn-toggle');
      toggle.type = 'checkbox';
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
      header.append(chevron, icon, label, availability, toggleWrap, menu);
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
    prompts.hidden = !!searchText && !slots.some(slot =>
      [slot.slot_name, slot.content].some(value =>
        String(value || '').toLowerCase().includes(searchText)));
  };
  search.addEventListener('input', filter);
  page.addEventListener('click', event => {
    if (!event.target.closest('.v3-mode-holder, .conn-toggle-wrap')) {
      page.querySelectorAll('.v3-mode-menu').forEach(item => { item.hidden = true; });
    }
  });
  page.append(overview, search, list, status);
  host.replaceChildren(page);
  window.lucide?.createIcons({ nodes: [...page.querySelectorAll('[data-lucide]:not(.lucide)')] });
  filter();
}

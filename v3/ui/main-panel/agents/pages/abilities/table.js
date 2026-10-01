// Ported from v2 ui/shared/js/agent-ability-table.js: grouped row skeleton.
const chevronSvg = () => '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>';
const iconHtml = (name, size = '18px') => `<i data-lucide="${name}" style="width:${size};height:${size}"></i>`;
export function buildGroup(groupId, name, icon, color, desc) {
  const groupEl = document.createElement('div');
  groupEl.className = 'ac-row ac-group';
  groupEl.id = 'ac-group-' + groupId;

  const head = buildGroupHead(groupId, name, icon, color, desc);
  const bodyEl = document.createElement('div');
  bodyEl.className = 'ac-group-body';
  bodyEl.id = 'ac-group-body-' + groupId;

  groupEl.appendChild(head);
  groupEl.appendChild(bodyEl);

  head.addEventListener('click', e => {
    if (e.target.closest('.ac-tri, button')) return;
    groupEl.classList.toggle('expanded');
    head.setAttribute('aria-expanded', String(groupEl.classList.contains('expanded')));
  });

  head.tabIndex = 0;
  head.setAttribute('role', 'button');
  head.setAttribute('aria-expanded', 'false');
  head.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      groupEl.classList.toggle('expanded');
      head.setAttribute('aria-expanded', String(groupEl.classList.contains('expanded')));
    }
  });
  return groupEl;
}

function buildGroupHead(groupId, name, icon, color, desc) {
  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-group-head';

  // Expand chevron — LEFT of the icon (mirrors admin-ability-table.js).
  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = color;
  iconWrap.innerHTML = iconHtml(icon, '18px');

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = name;
  label.querySelector('.ac-ability-desc').textContent = desc;

  head.appendChild(iconWrap);
  head.appendChild(label);
  return head;
}

export function setGroupStatus(group, abilities) {
  const head = group.querySelector('.ac-group-head');
  const status = document.createElement('span');
  status.className = 'ac-group-status';
  const active = abilities.filter(ability => ability.visibility !== 'off').length;
  status.dataset.state = active === 0 ? 'off' : active === abilities.length ? 'on' : 'mixed';
  status.textContent = status.dataset.state === 'on' ? 'Enabled' :
    status.dataset.state === 'mixed' ? 'Partial' : 'Disabled';
  status.title = `${active} of ${abilities.length} abilities enabled`;
  head.append(status);
}

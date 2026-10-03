// Ported from v2 ui/main-panel/agents/js/tab-abilities.js: Prompts group and viewer.
const chevronSvg = () => '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>';
const iconHtml = (name, size = '18px') => `<i data-lucide="${name}" style="width:${size};height:${size}"></i>`;
const _PROMPT_ICON_MAP = {
  system: 'terminal', agent: 'bot', user: 'user', skills: 'book', tasks: 'list-todo',
  misc: 'file-text', automation: 'clock', bootstrap_tools: 'wrench',
};

export async function buildPromptsGroup(request, agentId) {
  const groupEl = document.createElement('div');
  groupEl.className = 'ac-row ac-group ac-group-prompts';
  groupEl.id = 'ac-group-prompts';

  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-group-head';

  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = 'var(--brand)';
  iconWrap.innerHTML = iconHtml('file-text', '18px');
  head.appendChild(iconWrap);

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = '<div class="ac-ability-name">Prompts</div><div class="ac-ability-desc">Prompt slots configured for this agent</div>';
  head.appendChild(label);

  const countEl = document.createElement('span');
  countEl.className = 'ac-group-prompts-count';
  countEl.style.cssText = 'margin-left:auto;font-size:11px;color:var(--fg-3);padding-right:10px;';
  head.appendChild(countEl);

  groupEl.appendChild(head);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'ac-group-body';
  bodyEl.id = 'ac-group-body-prompts';
  bodyEl.innerHTML = '<div class="conn-loading" style="padding:12px;color:var(--fg-3);">Loading prompts…</div>';
  groupEl.appendChild(bodyEl);

  // The page-level Ask or Search filter owns this synthetic row's visibility.
  // NOTE: `data-agent-filter-self` is read by agent-ability-table.js's
  // `_runAgentFilter` — keep the two in sync (PREVENTIVE-COMMENTS).
  groupEl.dataset.agentFilterSelf = 'true';
  groupEl.dataset.agentFilterItem = ':scope > .ac-group-body > .prompt-item-wrap';

  let built = false;
  const ensureBody = async () => {
    if (built) return;
    built = true;
    try {
      const data = await request('/agents/' + encodeURIComponent(agentId) + '/slots');
      const slots = data.slots || [];
      const overrides = {};
      bodyEl.innerHTML = '';
      if (!slots.length) {
        const empty = document.createElement('div');
        empty.style.cssText = 'padding:14px;color:var(--fg-3);font-size:12px;';
        empty.textContent = 'No prompts configured for this agent.';
        bodyEl.appendChild(empty);
      } else {
        slots.forEach(slot => bodyEl.appendChild(_buildPromptItem(slot, overrides)));
        countEl.textContent = `${slots.length} prompt${slots.length === 1 ? '' : 's'}`;
        window.lucide?.createIcons({ nodes: [...bodyEl.querySelectorAll('[data-lucide]:not(.lucide)')] });
      }
    } catch (_) {
      bodyEl.innerHTML = '<div style="padding:12px;color:var(--danger);font-size:12px;">Prompts could not be loaded.</div>';
    }
  };

  head.addEventListener('click', (e) => {
    if (e.target.closest('.ac-tri, button')) return;
    const willExpand = !groupEl.classList.contains('expanded');
    groupEl.classList.toggle('expanded');
    if (willExpand) ensureBody();
  });

  // Hooks for the page-level composite filter (_runAgentsAbilitiesFilter): build
  // the body on demand, and drive the expanded state without a head click.
  groupEl._ensureBuilt = ensureBody;
  groupEl._setExpanded = (on) => {
    groupEl.classList.toggle('expanded', !!on);
    if (on) ensureBody();
  };

  return groupEl;
}

function _buildPromptItem(slot, overrides) {
  const slotId = slot.slot_name || slot.id || 'prompt';
  const isOverridden = overrides && slotId in overrides;
  const content = slot.override_content != null ? slot.override_content : (slot.content || '');

  // A collapsible row: the head keeps the icon + name + truncated preview +
  // lock/override badges; clicking it toggles a text-viewer panel that shows the
  // FULL prompt content (untruncated, pre-wrapped, scrollable) below.
  const item = document.createElement('div');
  item.className = 'prompt-item-wrap';
  item.dataset.promptSlot = slotId;

  const head = document.createElement('div');
  head.className = 'ac-ability-row prompt-item';
  head.style.cursor = 'pointer';

  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = 'var(--fg-2)';
  iconWrap.innerHTML = iconHtml(_PROMPT_ICON_MAP[slotId] || 'file-text', '16px');
  head.appendChild(iconWrap);

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  const name = document.createElement('div');
  name.className = 'ac-ability-name';
  name.textContent = slotId.replace(/_/g, ' ');
  label.appendChild(name);
  const preview = document.createElement('div');
  preview.className = 'ac-ability-desc';
  preview.textContent = (content || '').trim().replace(/\s+/g, ' ').slice(0, 160) || '—';
  label.appendChild(preview);
  head.appendChild(label);

  const badges = document.createElement('div');
  badges.style.cssText = 'display:flex;gap:6px;align-items:center;';
  if (slot.locked) {
    const b = document.createElement('span');
    b.style.cssText = 'font-size:10px;color:var(--accent);border:1px solid var(--accent-soft);border-radius:3px;padding:1px 6px;';
    b.textContent = 'Admin lock';
    badges.appendChild(b);
  }
  if (isOverridden) {
    const b = document.createElement('span');
    b.style.cssText = 'font-size:10px;color:var(--warning);border:1px solid var(--warning);border-radius:3px;padding:1px 6px;';
    b.textContent = 'Override';
    badges.appendChild(b);
  }
  if (badges.childNodes.length) head.appendChild(badges);

  item.appendChild(head);

  const body = document.createElement('div');
  body.className = 'prompt-viewer';
  body.style.cssText = 'padding:2px 4px 8px;';
  body.style.display = 'none';
  const viewer = document.createElement('pre');
  viewer.className = 'prompt-text-viewer';
  viewer.style.cssText = 'margin:0;padding:12px 14px;white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono,monospace);font-size:12px;line-height:1.6;color:var(--fg-2);background:var(--bg-1);border:1px solid var(--border);border-radius:8px;max-height:380px;overflow:auto;';
  viewer.textContent = content || '(empty prompt)';
  body.appendChild(viewer);
  item.appendChild(body);

  head.addEventListener('click', () => {
    const expanded = item.classList.toggle('expanded');
    body.style.display = expanded ? '' : 'none';
    chevron.style.transform = expanded ? 'rotate(90deg)' : '';
  });

  return item;
}

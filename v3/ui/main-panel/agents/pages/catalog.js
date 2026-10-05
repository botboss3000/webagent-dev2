'use strict';

// V3 mirrors V2's JSON directory and shared row styles, without V2 auth imports.
export async function loadAgentPages() {
  const response = await fetch(new URL('./catalog.json', import.meta.url), { cache: 'no-store' });
  if (!response.ok) throw Error('Agent page catalog unavailable');
  const catalog = await response.json();
  const ids = new Set();
  if (!Array.isArray(catalog.pages) || !catalog.pages.length) throw Error('Agent page catalog is empty');
  for (const page of catalog.pages) {
    if (!/^[a-z][a-z0-9-]*$/.test(page.id) || ids.has(page.id) || !page.title || !page.panelId) throw Error('Invalid agent page catalog');
    ids.add(page.id);
  }
  return [...catalog.pages].sort((a, b) => (a.order || 0) - (b.order || 0));
}

export function renderAgentDirectory(host, pages, onNavigate, favorites = [], onToggle = () => {}) {
  const landing = document.createElement('div');
  landing.className = 'agent-config-landing';
  landing.setAttribute('aria-label', 'Agent page directory');
  const group = document.createElement('section');
  group.className = 'agent-config-settings-group';
  const label = document.createElement('div');
  label.className = 'agent-config-settings-group-label';
  label.textContent = 'Core configuration';
  const topics = document.createElement('div');
  topics.className = 'agent-config-topics';
  for (const page of pages) {
    const row = document.createElement('section');
    row.className = 'agent-config-topic agent-config-topic-row';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agent-config-topic-heading';
    button.dataset.agentPage = page.id;
    button.setAttribute('aria-label', `Open ${page.title}`);
    const icon = document.createElement('span');
    icon.className = 'agent-config-topic-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 15h8"/></svg>';
    const copy = document.createElement('span');
    copy.className = 'agent-config-topic-copy';
    const title = document.createElement('span');
    title.className = 'agent-config-topic-title';
    title.textContent = page.title;
    const description = document.createElement('span');
    description.className = 'agent-config-topic-description';
    description.textContent = page.description || '';
    copy.append(title, description);
    const star = document.createElement('button');
    star.type = 'button';
    star.className = 'agent-config-topic-icon agent-config-star';
    star.setAttribute('aria-label', `Star ${page.title}`);
    const selected = favorites.includes(page.id);
    star.dataset.favoritePage = page.id;
    star.setAttribute('aria-pressed', String(selected));
    star.addEventListener('click', () => onToggle(page.id));
    star.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 2.78 5.63L21 9.54l-4.5 4.39 1.06 6.2L12 17.2l-5.56 2.93 1.06-6.2L3 9.54l6.22-.91L12 3z"/></svg>';
    star.querySelector('svg').setAttribute('fill', selected ? 'currentColor' : 'none');
    button.append(icon, copy);
    button.addEventListener('click', () => onNavigate(page.id));
    row.append(button, star);
    topics.append(row);
  }
  group.append(label, topics);
  landing.append(group);
  host.replaceChildren(landing);
}

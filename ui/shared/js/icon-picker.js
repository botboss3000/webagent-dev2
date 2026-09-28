'use strict';

/**
 * Shared, generic searchable Lucide icon picker.
 *
 * Opens a modal grid (search box + synonym matching) and resolves to the chosen
 * icon name, or null if cancelled. It has NO coupling to any app object — the
 * caller decides what to do with the result. Reused by the Agents page (pick an
 * agent icon) and App Settings (pick a page icon). The .icon-picker-* CSS lives
 * in ui/main-panel/agents/agents.css, which index.html loads globally, so the popover is
 * styled on every page.
 *
 * Usage:
 *   const chosen = await openIconPicker({ current: 'bot', title: 'Choose an icon' });
 *   if (chosen) { ... }   // null when the user cancels
 */

import { icon } from './icons.js';

export const ICON_PICKER_ICONS = [
  'bot', 'bot-message-square', 'brain', 'sparkles', 'cpu', 'zap',
  'radio', 'antenna', 'satellite', 'telescope',
  'message-square', 'message-circle', 'message-square-text', 'send', 'mail',
  'phone', 'megaphone', 'bell', 'bell-ring',
  'code', 'terminal', 'wrench', 'hammer', 'settings-2',
  'sliders', 'cog', 'book-open',
  'play', 'square-play', 'forward', 'rotate-cw', 'refresh-cw',
  'repeat', 'repeat2', 'shuffle', 'arrow-right-left',
  'database', 'hard-drive', 'folder', 'folder-open', 'file-text',
  'globe', 'search', 'eye', 'scan-search',
  'shield', 'shield-check', 'lock', 'key-round', 'fingerprint',
  'user-check', 'users', 'user-plus',
  'palette', 'paintbrush', 'image', 'camera', 'layout-dashboard',
  'layout-grid', 'panel-top', 'panel-left',
  'network', 'share-2', 'link', 'git-branch', 'git-merge',
  'workflow', 'tree-pine', 'layers',
  'calculator', 'divide', 'equal', 'variable', 'function-square',
  'sigma', 'pi', 'infinity',
  'check-circle', 'alert-circle', 'info', 'help-circle',
  'loader-2', 'activity', 'signal', 'wifi',
  'arrow-up', 'arrow-down', 'arrow-left', 'arrow-right',
  'chevrons-left-right', 'chevrons-up-down',
  'notepad-text', 'clipboard-list', 'list-checks', 'check-square',
  'pen-square', 'edit-3', 'bookmark', 'tags',
  'star', 'heart', 'heart-crack', 'heart-handshake', 'award', 'target', 'crosshair',
  'compass', 'map', 'map-pin', 'clock', 'timer', 'alarm-clock', 'hourglass',
  'cloud', 'sun', 'sunrise', 'sunset', 'sun-dim', 'sun-medium', 'moon', 'feather', 'lightbulb',
  'briefcase', 'shopping-cart', 'credit-card', 'coins', 'piggy-bank',
  'smile', 'smile-plus', 'laugh', 'frown', 'meh', 'angry',
  'thumbs-up', 'thumbs-down', 'hand-heart', 'hand-helping', 'handshake',
  'circle', 'triangle', 'diamond', 'hexagon', 'shapes', 'puzzle',
  'trees', 'leaf', 'flame', 'droplet', 'snowflake', 'stars', 'rainbow', 'umbrella', 'wind', 'waves',
  'trash-2', 'copy', 'save', 'download', 'upload', 'filter', 'pencil', 'command',
  'x', 'x-circle', 'stop-circle', 'more-horizontal', 'more-vertical', 'list', 'menu',
  'chevron-up', 'chevron-down', 'chevron-left', 'chevron-right', 'move', 'expand',
  'maximize-2', 'minimize-2', 'crop', 'scissors', 'power', 'toggle-left', 'toggle-right',
  'rocket', 'crown', 'gem', 'trophy', 'medal', 'dollar-sign', 'banknote', 'wallet', 'store', 'building-2',
  'bar-chart-3', 'pie-chart', 'trending-up', 'trending-down',
  'circuit-board', 'atom', 'wand-2', 'monitor', 'home', 'inbox',
  'cloud-sun', 'cloud-moon', 'cloud-lightning', 'cloud-rain', 'cloud-drizzle', 'cloud-fog', 'cloud-snow',
  'music', 'music-2', 'music-3', 'music-4', 'gamepad-2', 'dice-5',
  'user-round', 'users-round',
  'plane', 'navigation', 'earth',
  'phone-call', 'phone-forwarded', 'message-circle-question', 'life-buoy',
  'calendar-days', 'calendar-check',
  'book', 'graduation-cap', 'scroll',
  'undo-2', 'redo-2', 'panel-bottom', 'blocks', 'package', 'box', 'grid-3x3',
  'square', 'scan-face', 'zoom-in', 'fullscreen',
];

const ICON_SYNONYMS = {
  'smile':     ['smile', 'smile-plus', 'laugh'],
  'happy':     ['smile', 'smile-plus', 'laugh', 'sparkles'],
  'joy':       ['smile', 'smile-plus', 'laugh', 'heart', 'star'],
  'laugh':     ['laugh', 'smile', 'smile-plus'],
  'funny':     ['laugh', 'smile', 'smile-plus'],
  'sad':       ['frown', 'meh'],
  'frown':     ['frown'],
  'angry':     ['angry'],
  'mad':       ['angry'],
  'meh':       ['meh'],
  'neutral':   ['meh'],
  'face':      ['smile', 'smile-plus', 'laugh', 'frown', 'meh', 'angry'],
  'thumbs':    ['thumbs-up', 'thumbs-down'],
  'like':      ['thumbs-up', 'heart', 'heart-handshake'],
  'dislike':   ['thumbs-down'],
  'hand':      ['thumbs-up', 'thumbs-down', 'hand-heart', 'handshake', 'fingerprint'],
  'wave':      ['hand-heart', 'arrow-right-left'],
  'point':     ['target', 'crosshair', 'map-pin'],
  'save':      ['database', 'hard-drive', 'save', 'download'],
  'download':  ['download', 'save'],
  'upload':    ['upload', 'cloud', 'arrow-up'],
  'delete':    ['trash-2', 'x', 'x-circle'],
  'trash':     ['trash-2'],
  'edit':      ['pen-square', 'edit-3', 'pencil', 'settings-2'],
  'write':     ['pen-square', 'edit-3', 'pencil', 'notepad-text'],
  'send':      ['send', 'mail', 'forward', 'share-2'],
  'copy':      ['copy', 'duplicate', 'clipboard-list'],
  'search':    ['search', 'scan-search', 'eye'],
  'find':      ['search', 'scan-search', 'compass', 'map'],
  'filter':    ['filter', 'sliders'],
  'settings':  ['settings-2', 'cog', 'sliders', 'wrench'],
  'config':    ['settings-2', 'cog', 'sliders'],
  'lock':      ['lock', 'shield', 'shield-check', 'key-round'],
  'secure':    ['shield', 'shield-check', 'lock', 'fingerprint'],
  'key':       ['key-round', 'fingerprint'],
  'chat':      ['message-square', 'message-circle', 'message-square-text', 'bot-message-square', 'send', 'mail'],
  'message':   ['message-square', 'message-circle', 'message-square-text', 'bot-message-square', 'send', 'mail'],
  'talk':      ['message-square', 'message-circle', 'phone', 'megaphone'],
  'call':      ['phone', 'phone-call', 'phone-forwarded'],
  'mail':      ['mail', 'send', 'inbox'],
  'email':     ['mail', 'send', 'inbox'],
  'notify':    ['bell', 'bell-ring', 'alert-circle', 'info'],
  'alert':     ['alert-circle', 'bell', 'bell-ring', 'info', 'warning'],
  'warning':   ['alert-circle', 'help-circle', 'info'],
  'sun':       ['sun', 'sunrise', 'sunset', 'sun-dim'],
  'moon':      ['moon', 'cloud-moon'],
  'star':      ['star', 'stars', 'sparkles', 'award'],
  'cloud':     ['cloud', 'cloud-sun', 'cloud-moon', 'cloud-lightning', 'cloud-rain'],
  'rain':      ['cloud-rain', 'cloud-drizzle', 'cloud-lightning', 'umbrella'],
  'fire':      ['flame', 'zap'],
  'water':     ['droplet', 'waves', 'cloud-rain', 'umbrella'],
  'tree':      ['trees', 'tree-pine', 'palm-tree'],
  'code':      ['code', 'terminal', 'git-branch', 'git-merge', 'workflow'],
  'dev':       ['code', 'terminal', 'cpu', 'database', 'wrench'],
  'terminal':  ['terminal', 'code', 'command'],
  'git':       ['git-branch', 'git-merge', 'workflow'],
  'branch':    ['git-branch', 'git-merge'],
  'robot':     ['bot', 'bot-message-square', 'cpu', 'brain', 'circuit-board'],
  'ai':        ['bot', 'brain', 'cpu', 'sparkles', 'bot-message-square'],
  'brain':     ['brain', 'circuit-board'],
  'shape':     ['shapes', 'circle', 'square', 'triangle', 'hexagon', 'diamond'],
  'circle':    ['circle', 'radio', 'target'],
  'box':       ['box', 'blocks', 'package'],
  'grid':      ['layout-grid', 'grid-3x3'],
  'layout':    ['layout-dashboard', 'layout-grid', 'panel-top', 'panel-left'],
  'money':     ['coins', 'credit-card', 'piggy-bank', 'banknote', 'dollar-sign'],
  'payment':   ['credit-card', 'coins', 'wallet', 'shopping-cart'],
  'shop':      ['shopping-cart', 'store', 'credit-card'],
  'cart':      ['shopping-cart'],
  'business':  ['briefcase', 'building-2', 'banknote', 'coins'],
  'work':      ['briefcase', 'building-2', 'clock', 'calendar-days'],
  'time':      ['clock', 'timer', 'alarm-clock', 'hourglass', 'calendar-days'],
  'clock':     ['clock', 'alarm-clock', 'timer', 'hourglass'],
  'calendar':  ['calendar-days', 'calendar-check', 'clock'],
  'user':      ['users', 'user-plus', 'user-check', 'user-round'],
  'people':    ['users', 'users-round', 'user-plus'],
  'group':     ['users', 'users-round', 'network'],
  'home':      ['home', 'compass', 'map'],
  'map':       ['map', 'map-pin', 'compass', 'globe', 'navigation'],
  'world':     ['globe', 'network', 'earth', 'map'],
  'travel':    ['compass', 'map', 'map-pin', 'globe', 'plane'],
  'learn':     ['book-open', 'book', 'bookmark', 'graduation-cap', 'scroll'],
  'book':      ['book-open', 'bookmark', 'notepad-text', 'scroll'],
  'tool':      ['wrench', 'hammer', 'settings-2', 'cog', 'sliders'],
  'build':     ['wrench', 'hammer', 'hard-drive', 'cpu', 'blocks'],
  'idea':      ['lightbulb', 'sparkles', 'brain', 'wand-2', 'atom'],
  'creative':  ['palette', 'paintbrush', 'image', 'camera', 'wand-2', 'feather'],
  'art':       ['palette', 'paintbrush', 'image', 'camera', 'feather'],
  'photo':     ['image', 'camera', 'scan-search'],
  'game':      ['gamepad-2', 'dice-5', 'puzzle', 'shapes'],
  'puzzle':    ['puzzle', 'shapes'],
  'target':    ['target', 'crosshair', 'award'],
  'goal':      ['target', 'crosshair', 'award'],
  'win':       ['award', 'trophy', 'medal', 'star', 'crown'],
  'award':     ['award', 'trophy', 'medal', 'crown', 'star'],
  'heart':     ['heart', 'heart-handshake', 'heart-crack'],
  'love':      ['heart', 'heart-handshake', 'sparkles'],
  'friend':    ['users', 'user-plus', 'handshake', 'heart-handshake'],
  'help':      ['help-circle', 'life-buoy', 'message-circle-question'],
  'question':  ['help-circle', 'message-circle-question'],
  'info':      ['info', 'alert-circle', 'bell'],
  'check':     ['check-circle', 'check-square', 'list-checks', 'shield-check'],
  'done':      ['check-circle', 'check-square', 'list-checks'],
  'on':        ['toggle-left', 'toggle-right', 'power'],
  'off':       ['toggle-left', 'power', 'x-circle'],
  'power':     ['power', 'zap', 'loader-2', 'activity'],
  'loading':   ['loader-2', 'activity', 'refresh-cw', 'rotate-cw'],
  'refresh':   ['refresh-cw', 'rotate-cw', 'repeat', 'repeat2'],
  'sync':      ['refresh-cw', 'repeat2', 'arrow-right-left', 'shuffle'],
  'play':      ['play', 'square-play', 'forward', 'shuffle'],
  'stop':      ['stop-circle', 'square', 'x-circle'],
  'link':      ['link', 'share-2', 'git-branch', 'network'],
  'more':      ['more-horizontal', 'more-vertical', 'expand', 'list'],
  'menu':      ['menu', 'list', 'more-horizontal', 'panel-top'],
  'top':       ['arrow-up', 'chevron-up', 'panel-top', 'trending-up', 'crown', 'award', 'star'],
  'down':      ['arrow-down', 'chevron-down', 'panel-bottom', 'trending-down'],
  'left':      ['arrow-left', 'chevron-left', 'panel-left', 'undo-2'],
  'right':     ['arrow-right', 'chevron-right', 'panel-right', 'redo-2'],
  'move':      ['arrow-right-left', 'chevrons-up-down', 'expand', 'move'],
  'expand':    ['expand', 'maximize-2', 'fullscreen', 'zoom-in'],
  'crop':      ['crop', 'scissors', 'minimize-2'],
  'print':     ['print', 'printer'],
  'camera':    ['camera', 'image', 'video', 'scan-search'],
  'music':     ['music', 'music-2', 'music-3', 'music-4', 'radio'],
  'rocket':    ['rocket', 'space', 'zap', 'sparkles'],
  'fast':      ['zap', 'rocket', 'forward', 'arrow-right'],
  'slow':      ['timer', 'clock', 'snail'],
  'new':       ['sparkles', 'star', 'award', 'gem'],
  'data':      ['database', 'hard-drive', 'file-text', 'bar-chart-3'],
  'chart':     ['bar-chart-3', 'pie-chart', 'trending-up', 'activity'],
  'graph':     ['activity', 'trending-up', 'bar-chart-3', 'sigma'],
  'page':      ['file-text', 'book-open', 'layout-dashboard', 'panel-top'],
  'admin':     ['wrench', 'shield', 'settings-2', 'sliders'],
};

function matchIcons(query) {
  const q = query ? query.toLowerCase().trim() : '';
  if (!q) return ICON_PICKER_ICONS;
  const matched = new Set();
  for (const name of ICON_PICKER_ICONS) {
    if (name.includes(q)) matched.add(name);
  }
  for (const [keyword, icons] of Object.entries(ICON_SYNONYMS)) {
    if (keyword.startsWith(q) || keyword.includes(q)) {
      for (const iconName of icons) {
        if (ICON_PICKER_ICONS.includes(iconName)) matched.add(iconName);
      }
    }
  }
  return [...matched].sort();
}

/**
 * Open the icon picker. Resolves to the chosen icon name, or null if cancelled.
 * @param {{current?: string, title?: string}} [opts]
 * @returns {Promise<string|null>}
 */
export function openIconPicker(opts = {}) {
  const { current = '', title = 'Choose an icon' } = opts;

  return new Promise((resolve) => {
    const old = document.getElementById('shared-icon-picker');
    if (old) old.remove();

    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      resolve(value);
    };

    const backdrop = document.createElement('div');
    backdrop.id = 'shared-icon-picker';
    backdrop.className = 'icon-picker-backdrop';

    const popover = document.createElement('div');
    popover.className = 'icon-picker-popover';
    popover.innerHTML = `
      <div class="icon-picker-header">
        <span class="icon-picker-title">${title}</span>
        <div class="icon-picker-header-actions">
          <button class="icon-picker-save" title="Apply selected icon">${icon('check', { size: '18px' })}</button>
          <button class="icon-picker-close" title="Close">✕</button>
        </div>
      </div>
      <div class="icon-picker-search-wrap">
        <input type="text" class="icon-picker-search" placeholder="Search icons…" autofocus>
      </div>
      <div class="icon-picker-grid"></div>
    `;
    backdrop.appendChild(popover);
    document.body.appendChild(backdrop);

    const grid = popover.querySelector('.icon-picker-grid');
    const search = popover.querySelector('.icon-picker-search');
    const closeBtn = popover.querySelector('.icon-picker-close');
    const saveBtn = popover.querySelector('.icon-picker-save');

    search.focus();
    let currentIcon = current || '';

    function renderIcons(filter) {
      grid.innerHTML = '';
      const list = matchIcons(filter);
      if (!list.length) {
        grid.innerHTML = '<div class="icon-picker-empty">No icons match</div>';
        return;
      }
      for (const name of list) {
        const item = document.createElement('div');
        item.className = 'icon-picker-item' + (name === currentIcon ? ' selected' : '');
        item.title = name;
        item.innerHTML = icon(name, { size: '22px' });
        item.addEventListener('click', (e) => {
          e.stopPropagation();
          grid.querySelectorAll('.icon-picker-item.selected').forEach(el => el.classList.remove('selected'));
          item.classList.add('selected');
          currentIcon = name;
        });
        item.addEventListener('dblclick', (e) => {
          e.stopPropagation();
          currentIcon = name;
          finish(currentIcon);
        });
        grid.appendChild(item);
      }
    }

    search.addEventListener('input', () => renderIcons(search.value));
    renderIcons('');

    saveBtn.addEventListener('click', () => { if (currentIcon) finish(currentIcon); });
    closeBtn.addEventListener('click', () => finish(null));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish(null); });

    function onKey(e) {
      if (e.key === 'Enter') {
        if (e.target === search) { search.blur(); return; }
        if (currentIcon) finish(currentIcon);
      } else if (e.key === 'Escape') {
        finish(null);
      }
    }
    document.addEventListener('keydown', onKey);
  });
}

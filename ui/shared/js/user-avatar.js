'use strict';

/**
 * Shared circular letter-icon component.
 *
 * Renders a <span class="user-avatar" data-size="sm|md|lg">A</span> using
 * the first character of display_name (falling back to username). Background
 * is the same neutral accent across all users (no hash-coloring) per the
 * design choice for this app.
 */

/** First letter, uppercased, or '?' fallback. */
function letterFor(account) {
  if (!account) return '?';
  const src = (account.display_name || account.username || '').trim();
  if (!src) return '?';
  const ch = src[0];
  return ch.toUpperCase();
}

/**
 * Build an avatar element.
 * @param {object} account — { username, display_name } at minimum
 * @param {'sm'|'md'|'lg'} [size='md']
 * @param {string} [extraClass='']
 */
export function renderAvatar(account, size = 'md', extraClass = '') {
  const span = document.createElement('span');
  span.className = 'user-avatar' + (extraClass ? ' ' + extraClass : '');
  span.dataset.size = size;
  span.textContent = letterFor(account);
  const full = (account && (account.display_name || account.username)) || '';
  if (full) span.title = full;
  return span;
}

/** Same as renderAvatar but returns an HTML string. */
function avatarHtml(account, size = 'md', extraClass = '') {
  const letter = letterFor(account).replace(/</g, '&lt;');
  const cls = 'user-avatar' + (extraClass ? ' ' + extraClass : '');
  return `<span class="${cls}" data-size="${size}">${letter}</span>`;
}

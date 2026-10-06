'use strict';

let menu = null;
let outside = null;

export function authHeaders() {
  const token = window.__v3FilesCsrf || '';
  return token ? {'x-csrf-token': token} : {};
}
export function isAdmin() { return window.__v3FilesAdmin === true; }
export function apiPath(path) { return path; }
export function _esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  })[ch]);
}
export async function copyText(value) {
  await navigator.clipboard.writeText(String(value));
}
export function _refreshLucideIcons() {
  window.lucide?.createIcons?.();
}
export function closeFloatingMenu() {
  if (outside) {
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', onMenuKey, true);
    outside = null;
  }
  menu?.remove();
  menu = null;
}
function onMenuKey(event) {
  if (event.key === 'Escape') closeFloatingMenu();
}
export function openFloatingMenu(items, top, left) {
  closeFloatingMenu();
  const host = document.createElement('div');
  host.className = 'files-tab-menu';
  host.id = 'files-floating-menu';
  host.setAttribute('role', 'menu');
  for (const item of items.filter(entry => !/production/i.test(entry.label || ''))) {
    if (item.separator) {
      const line = document.createElement('div');
      line.className = 'files-tab-menu-sep';
      host.append(line);
      continue;
    }
    if (item.info) {
      const row = document.createElement('div');
      row.className = 'files-tab-menu-info';
      if (item.id) row.id = item.id;
      row.textContent = item.label || '';
      host.append(row);
      continue;
    }
    if (item.field) {
      const wrap = document.createElement('div');
      wrap.className = 'files-tab-menu-field';
      const label = document.createElement('label');
      label.textContent = item.label || '';
      const input = document.createElement('input');
      input.type = item.fieldType || 'text';
      input.value = item.value || '';
      input.placeholder = item.placeholder || '';
      input.autocomplete = 'off';
      input.spellcheck = false;
      if (item.fieldKey) input.dataset.field = item.fieldKey;
      input.addEventListener('input', () => item.onInput?.(input.value));
      input.addEventListener('change', () => item.onSave?.(input.value));
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          item.onSave?.(input.value);
          input.blur();
        }
      });
      wrap.append(label, input);
      host.append(wrap);
      continue;
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'files-tab-menu-item' + (item.danger ? ' danger' : '') +
      (item.checked ? ' checked' : '');
    button.setAttribute('role', 'menuitem');
    button.disabled = !!item.disabled;
    if (item.title) button.title = item.title;
    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', item.icon || 'circle');
    icon.className = 'lucide-icon';
    const label = document.createElement('span');
    label.textContent = item.label || '';
    const check = document.createElement('span');
    check.className = 'files-tab-menu-check';
    check.textContent = '✓';
    button.append(icon, label, check);
    button.addEventListener('click', event => {
      event.stopPropagation();
      closeFloatingMenu();
      item.action?.();
    });
    host.append(button);
  }
  document.body.append(host);
  const rect = host.getBoundingClientRect();
  host.style.top = Math.max(8, Math.min(window.innerHeight - rect.height - 8, top)) + 'px';
  host.style.left = Math.max(8, Math.min(window.innerWidth - rect.width - 8, left)) + 'px';
  _refreshLucideIcons();
  outside = event => { if (!host.contains(event.target)) closeFloatingMenu(); };
  queueMicrotask(() => {
    if (outside) {
      document.addEventListener('pointerdown', outside, true);
      document.addEventListener('keydown', onMenuKey, true);
    }
  });
}
export function applyRubberBand() {}
function key(primary, fallback) { return 'v3:file-manager:' + (primary || fallback); }
export function kvRead(primary, fallback) {
  try { return JSON.parse(localStorage.getItem(key(primary, fallback))); } catch { return null; }
}
export function kvWrite(primary, fallback, value) {
  try { localStorage.setItem(key(primary, fallback), JSON.stringify(value)); } catch {}
}
export function kvDelete(primary, fallback) {
  try { localStorage.removeItem(key(primary, fallback)); } catch {}
}

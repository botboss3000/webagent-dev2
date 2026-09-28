'use strict';

import { apiPath } from '../../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { _fetch, _esc } from '../../settings/utils.js';

const SCOPES = [
  ['main', 'Main panel pages', 'Controls which app destinations each audience can open.'],
  ['admin', 'Admin panel pages', 'Admin-tool views remain protected by backend role checks.'],
];

const AUDIENCES = [
  ['all', 'Everyone'],
  ['auth', 'Registered users'],
  ['off', 'Admins only'],
];

function _visibility(page) {
  if (['all', 'auth', 'off'].includes(page.visibility)) return page.visibility;
  return page.hidden ? 'off' : 'auth';
}

async function _catalog(force) {
  if (force && typeof window.__reloadPagesCatalog === 'function') {
    return window.__reloadPagesCatalog();
  }
  if (typeof window.__loadPagesCatalog === 'function') {
    return window.__loadPagesCatalog();
  }
  const response = await _fetch(apiPath('/api/v1/pages/catalog'), { cache: 'no-store' });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}

async function _save(scope, pageId, visibility) {
  const response = await _fetch(
    apiPath('/admin/integrations/pages/' + scope + '/' + encodeURIComponent(pageId)),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visibility: visibility }),
    },
  );
  if (!response.ok) throw new Error('HTTP ' + response.status);
}

function _applyLive(scope, catalog) {
  if (scope === 'main' && typeof window.__buildHeader === 'function') {
    try { window.__buildHeader(catalog.main || []); } catch (_) {}
  }
  if (scope === 'admin' && typeof window.__applyAdminPanelOrder === 'function') {
    try { window.__applyAdminPanelOrder(); } catch (_) {}
  }
}

function _renderScope(scope, title, hint, pages, catalog) {
  const block = document.createElement('div');
  block.className = 'user-policy-scope';
  block.innerHTML = '<div class="user-policy-scope-head"><div><strong>' + _esc(title)
    + '</strong><span>' + _esc(hint) + '</span></div></div>'
    + '<div class="user-policy-page-list"></div>';
  const list = block.querySelector('.user-policy-page-list');

  for (const page of pages || []) {
    const row = document.createElement('label');
    row.className = 'user-policy-page-row';
    const current = _visibility(page);
    const options = AUDIENCES
      .filter(function (entry) { return !(page.locked && entry[0] === 'off'); })
      .map(function (entry) {
        return '<option value="' + entry[0] + '"' + (entry[0] === current ? ' selected' : '')
          + '>' + _esc(entry[1]) + '</option>';
      }).join('');
    row.innerHTML = '<span class="user-policy-page-name"><i data-lucide="'
      + _esc(page.icon || 'square') + '"></i><span><b>' + _esc(page.label || page.id)
      + '</b><small>' + _esc(page.id) + (page.locked ? ' · protected' : '')
      + '</small></span></span><select aria-label="Audience for ' + _esc(page.label || page.id)
      + '">' + options + '</select><span class="user-policy-save" role="status"></span>';

    const select = row.querySelector('select');
    const status = row.querySelector('.user-policy-save');
    select.addEventListener('change', async function () {
      if (!isAdmin()) {
        showRestrictedModal();
        select.value = current;
        return;
      }
      const previous = page.visibility || current;
      select.disabled = true;
      status.textContent = 'Saving…';
      try {
        await _save(scope, page.id, select.value);
        page.visibility = select.value;
        const fresh = await _catalog(true);
        catalog.main = fresh.main || [];
        catalog.admin = fresh.admin || [];
        _applyLive(scope, catalog);
        status.textContent = 'Saved';
      } catch (error) {
        select.value = previous;
        status.textContent = 'Could not save';
      } finally {
        select.disabled = false;
        setTimeout(function () { status.textContent = ''; }, 1800);
      }
    });
    list.appendChild(row);
  }
  return block;
}

export async function mountPageAccess(host) {
  if (!host) return;
  host.innerHTML = '<section class="members-policy user-policy-card"><div class="members-policy-title">'
    + 'Page access</div><div class="members-loading">Loading page audiences…</div></section>';
  try {
    const catalog = await _catalog(false);
    const card = document.createElement('section');
    card.className = 'members-policy user-policy-card';
    card.innerHTML = '<div class="members-policy-title">Page access</div>'
      + '<p class="user-policy-help">This is the installation-wide audience gate. A registered user also needs the page in their assigned experience tier. Administrators always retain full access.</p>';
    for (const scope of SCOPES) {
      card.appendChild(_renderScope(scope[0], scope[1], scope[2], catalog[scope[0]] || [], catalog));
    }
    host.replaceChildren(card);
    if (window.lucide) { try { window.lucide.createIcons(); } catch (_) {} }
  } catch (error) {
    host.innerHTML = '<section class="members-policy user-policy-card"><div class="members-policy-title">'
      + 'Page access</div><div class="members-empty">Page policy unavailable: '
      + _esc(error.message) + '</div></section>';
  }
}

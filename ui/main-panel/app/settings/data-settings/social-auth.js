'use strict';

/**
 * Social Sign-In — App Configuration → Data Settings → App Access card.
 *
 * The drop-in social-login providers (Google / GitHub / Microsoft / Apple …),
 * discovered under plugins/auth_providers/ and served by
 * GET /api/v1/auth/social/admin/providers. This is the former Users → Social
 * Sign-In card, moved here to sit INSIDE the App Access group: the access-mode
 * radios say who may register, these providers say how they can sign in — one
 * "how people get into this instance" section. Each provider is an expandable
 * row: an Enabled toggle (auto-saves), its Client ID / Secret fields, the exact
 * Redirect URI to register, and a Save button.
 *
 * Self-contained — like app-access.js, it owns its own load + save and exposes
 * window.__refreshSocialAuth (called on section-show in nav.js). It renders into
 * #ac-social-list which now lives in data-settings.html (App Access card). The
 * .ac-social-* styling is global (ui/shared/css/app3.css), so nothing but the
 * container moved. Admin-only; a non-admin save is reverted.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { _fetch, _qs, _esc, _flashCheck } from '../utils.js';
import { socialIconSvg } from '../../../../shared/js/social-icons.js';
import { copyText } from '../../../../shared/js/clipboard.js';
import { _tipBadge } from './deploy.js';   // shared "?" field-help badge (same as the cloud-deploy fields)

/** Reflect the enabled-provider count into the "Sign in with" header badge, the
 *  same "N on" summary an ability-group header carries. Empty when there are no
 *  providers (or the list failed to load). */
function _updateCount() {
  const badge = _qs('ac-social-count');
  if (!badge) return;
  const toggles = document.querySelectorAll('#ac-social-list .conn-toggle');
  if (!toggles.length) { badge.textContent = ''; return; }
  const on = [...toggles].filter(t => t.checked).length;
  badge.textContent = `${on} on`;
}

// Turn every rendered field label carrying a `data-tip` into a circled "?" help
// badge (the shared badge from deploy.js, using the one shared floating bubble).
// Idempotent — a `tipWired` flag stops re-renders from stacking badges.
function _wireSocialTips() {
  document.querySelectorAll('#ac-social-list .ac-social-flabel[data-tip]').forEach(lab => {
    if (lab.dataset.tipWired) return;
    lab.dataset.tipWired = '1';
    const badge = _tipBadge(lab.dataset.tip);
    if (badge) lab.appendChild(badge);
  });
}

async function _loadSocialProviders() {
  const list = _qs('ac-social-list');
  if (!list) return;
  try {
    const res = await _fetch(apiPath('/api/v1/auth/social/admin/providers'), { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const providers = data.providers || [];
    if (!providers.length) {
      list.innerHTML = `<div class="ac-table-empty" class="ac-p14">No providers installed. Drop a folder into <code>plugins/auth_providers/</code> to add one.</div>`;
      _updateCount();
      return;
    }
    list.innerHTML = providers.map(_renderSocialProvider).join('');
    providers.forEach(p => _wireSocialProvider(list.querySelector(`[data-pid="${CSS.escape(p.id)}"]`), p));
    _wireSocialTips();
    _updateCount();
  } catch (e) {
    list.innerHTML = `<div class="ac-table-empty" class="ac-p14 ac-fg-danger">Could not load providers: ${_esc(e.message)}</div>`;
    _updateCount();
  }
}

function _renderSocialProvider(p) {
  const statusBadge = p.configured
    ? `<span class="ac-um-badge ac-um-badge-approved">configured</span>`
    : `<span class="ac-um-badge ac-um-badge-pending">needs keys</span>`;
  const fields = (p.credential_fields || []).map(f => {
    const type = f.secret ? 'password' : 'text';
    const ph = f.secret
      ? (f.is_set ? '•••••••• (saved — leave blank to keep)' : 'Not set')
      : '';
    const val = f.secret ? '' : _esc(f.value || '');
    // A per-field "where to get this" hint (from the provider descriptor's
    // credential_fields[].tip) becomes a "?" badge on the label — wired after
    // render by _wireSocialTips, exactly like the cloud-deploy fields.
    const tipAttr = f.tip ? ` data-tip="${_esc(f.tip)}"` : '';
    return `<label class="ac-social-flabel"${tipAttr}>${_esc(f.label)}</label>
      <input class="ac-social-field" data-key="${_esc(f.key)}" type="${type}"
             autocomplete="off" spellcheck="false" placeholder="${_esc(ph)}" value="${val}">`;
  }).join('');
  const setup = p.setup_url
    ? `<a href="${_esc(p.setup_url)}" target="_blank" rel="noopener" class="ac-social-setup">Where to get these →</a>`
    : '';
  const requires = (p.requires || []).length
    ? `<div class="ac-social-req">${_esc(p.requires.join('; '))}</div>` : '';
  const hint = p.setup_hint ? `<div class="ac-social-hint">${_esc(p.setup_hint)}</div>` : '';

  // An individual ability-style row: `.ac-row` wrapper (so the shared
  // `.ac-row.expanded > .ac-ability-body` reveal + chevron-rotate rules apply) →
  // `.ac-ability-row` head with a left chevron, the brand icon in the shared icon
  // column, name + live substate, a status cell for the save tick, and the shared
  // `.conn-toggle` Enabled switch → expandable `.ac-ability-body` holding the keys.
  return `<div class="ac-row ac-social-prov" data-pid="${_esc(p.id)}">
    <div class="ac-ability-row" data-role="expand">
      <span class="ac-row-chevron ac-row-chevron--left"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg></span>
      <span class="ac-ability-icon ac-social-brand">${socialIconSvg(p.icon)}</span>
      <div class="ac-ability-label">
        <div class="ac-ability-name">${_esc(p.display_name)} ${statusBadge}</div>
        <div class="ac-ability-desc" data-role="substate">${p.enabled ? 'Shown on the sign-in screen' : 'Off'}</div>
      </div>
      <span class="ac-ability-status ac-social-check" data-role="rowcheck"></span>
      <label class="conn-toggle-wrap ac-ability-toggle-wrap" title="Show this on the sign-in screen">
        <input type="checkbox" class="conn-toggle" data-role="enable" ${p.enabled ? 'checked' : ''}>
        <span class="conn-toggle-track"></span>
      </label>
    </div>
    <div class="ac-ability-body">
      <div class="ac-social-detail">
        ${fields}
        <label class="ac-social-flabel" data-tip="Copy this exact URL into ${_esc(p.display_name)}'s allowed redirect / callback setting. It must match character-for-character — same https, host and path, with no trailing slash.">Redirect URI (register this with ${_esc(p.display_name)})</label>
        <div class="ac-social-redirect">
          <code data-role="redirect">${_esc(p.redirect_uri || '')}</code>
          <button type="button" class="ac-um-action-btn" data-role="copy">Copy</button>
        </div>
        ${requires}${hint}${setup}
        <div class="ac-social-actions">
          <button type="button" class="ac-btn ac-btn-primary" data-role="save">Save keys</button>
        </div>
      </div>
    </div>
  </div>`;
}

function _wireSocialProvider(el, p) {
  if (!el) return;
  // Expand/collapse the row body when the head is clicked — but NOT when the click
  // lands on the Enabled switch. Toggles `.expanded` on the `.ac-row` so the shared
  // reveal + left-chevron-rotate rules (app3.css) drive the open/close, exactly
  // like an ability row.
  el.querySelector('[data-role="expand"]')?.addEventListener('click', (ev) => {
    if (ev.target.closest('.conn-toggle-wrap')) return;
    el.classList.toggle('expanded');
  });
  // Enable toggle auto-saves (keeps stored keys — sends no field values).
  el.querySelector('[data-role="enable"]')?.addEventListener('change', () => _saveSocialProvider(el, p.id, true));
  el.querySelector('[data-role="save"]')?.addEventListener('click', () => _saveSocialProvider(el, p.id, false));
  el.querySelector('[data-role="copy"]')?.addEventListener('click', () => {
    const code = el.querySelector('[data-role="redirect"]');
    if (code) copyText(code.textContent || '');
  });
}

async function _saveSocialProvider(el, id, enabledOnly) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const check = el.querySelector('[data-role="rowcheck"]');
  const enabled = !!el.querySelector('[data-role="enable"]')?.checked;
  const fields = {};
  if (!enabledOnly) {
    el.querySelectorAll('.ac-social-field').forEach(inp => {
      // A blank secret means "keep the stored one" — only send non-empty values
      // for password fields; always send text fields (so they can be cleared).
      if (inp.type === 'password') {
        if (inp.value) fields[inp.dataset.key] = inp.value;
      } else {
        fields[inp.dataset.key] = inp.value;
      }
    });
  }
  if (check) { check.classList.remove('saved', 'error'); check.innerHTML = '<span class="agents-spinner"></span>'; }
  try {
    const res = await _fetch(apiPath(`/api/v1/auth/social/admin/providers/${encodeURIComponent(id)}`), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, fields }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const row = data.provider;
    _flashCheck(check, true);
    // Reflect the server's authoritative state (configured badge, substate,
    // and clear the just-saved secret inputs so they show the saved placeholder).
    if (row) {
      const badge = el.querySelector('.ac-ability-name .ac-um-badge');
      if (badge) {
        badge.className = 'ac-um-badge ' + (row.configured ? 'ac-um-badge-approved' : 'ac-um-badge-pending');
        badge.textContent = row.configured ? 'configured' : 'needs keys';
      }
      const sub = el.querySelector('[data-role="substate"]');
      if (sub) sub.textContent = row.enabled ? 'Shown on the sign-in screen' : 'Off';
      // Keep the toggle honest with the server + refresh the header "N on" count.
      const tog = el.querySelector('[data-role="enable"]');
      if (tog) tog.checked = !!row.enabled;
      _updateCount();
      if (!enabledOnly) {
        el.querySelectorAll('.ac-social-field').forEach(inp => {
          if (inp.type === 'password' && inp.value) {
            inp.value = '';
            inp.placeholder = '•••••••• (saved — leave blank to keep)';
          }
        });
      }
    }
  } catch (e) {
    // A failed enable/disable must not leave the switch lying — revert it to the
    // prior state and re-sync the count; then flag the error.
    if (enabledOnly) {
      const tog = el.querySelector('[data-role="enable"]');
      if (tog) tog.checked = !enabled;
      _updateCount();
    }
    _flashCheck(check, false, e.message);
  }
}

export function initSocialAuth() {
  _loadSocialProviders();
  // Re-read whenever the Data Settings section is shown (wired in nav.js).
  window.__refreshSocialAuth = _loadSocialProviders;
}

'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Reference design-system CSS variables; never hard-code a hex/rgb literal here.

/**
 * My appearance — the per-user theme editor on the account page.
 *
 * The self-service sibling of the admin Appearance panel
 * (ui/admin-tools/instances/app-config/app-settings/app-settings.js): a Light/Dark theme
 * table (preset chips + animated-background select + the full colour palette as
 * swatches) plus a shared fonts/border card and a "Reset my theme" button. It
 * appears on the Manage Account tab (ui/shared/js/account.js) ONLY when the admin
 * has turned on "Allow per-user themes" (App Settings → Appearance).
 *
 * Storage is the signed-in user's own profile via /api/v1/auth/me/appearance
 * (GET/PUT/DELETE). Each change PUTs a sparse patch; GET /api/v1/auth/ui-config
 * layers that patch on top of the global theme for this user on every device, so
 * anything they don't customise still follows the app's theme. Previews are live
 * in the user's own window via window.WA_APPEARANCE / window.WA_BG, exactly like
 * the admin panel.
 *
 * MIRROR: this table's structure must stay matched with the admin builder
 * (_buildThemeCard in app-settings.js). The token list, presets and colour maths
 * are the SHARED single source in ui/shared/js/appearance-editor.js — import them
 * here, never redefine, so the two editors can never drift.
 */

import { getActive } from './accounts.js';
import { _esc, _markSaving, _flashSaveCheck } from './dom-utils.js';
import {
  TOKEN_META, APP_DEFAULTS, PRESETS, FOLLOW_ALPHA,
  keyFor, asHex, splitColor, combineColor,
} from './appearance-editor.js';

const API = '/api/v1/auth/me/appearance';
const BG_API = '/api/v1/auth/backgrounds';

function _authHeaders() {
  const a = getActive();
  return a && a.access_token ? { 'Authorization': `Bearer ${a.access_token}` } : {};
}

// Font/border options — mirror of the admin Design table selects.
const _SANS_OPTS = [
  ["'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif", 'Inter (default)'],
  ["-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif", 'System'],
  ["'Trebuchet MS', 'Segoe UI', Verdana, sans-serif", 'Rounded'],
  ["Georgia, 'Times New Roman', serif", 'Serif'],
];
const _MONO_OPTS = [
  ["'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace", 'JetBrains Mono (default)'],
  ['ui-monospace, Menlo, Consolas, monospace', 'System mono'],
  ["'Courier New', Courier, monospace", 'Courier'],
];
const _BORDER_OPTS = [['0', 'Off — no borders'], ['1px', 'Thin'], ['2px', 'Medium'], ['3px', 'Thick']];

const _CHEVRON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>';

function _optionsHtml(opts, current) {
  return opts.map(([v, label]) => {
    const has = String(v) === String(current);
    return `<option value="${_esc(v)}"${has ? ' selected' : ''}>${_esc(label)}</option>`;
  }).join('');
}

/**
 * Build + wire the "My appearance" editor into `slotEl` (an empty container the
 * account page placed where the section should sit). When the feature is off,
 * the slot is removed so nothing shows. Fire-and-forget (async); fills the slot
 * once values + background catalog load.
 */
export async function mountMyAppearance(slotEl) {
  if (!slotEl) return;

  // 1) Gate on the admin flag + load the user's own overrides.
  let allow = false;
  let myOverrides = {};
  try {
    const r = await fetch(API, { headers: _authHeaders() });
    if (r.ok) {
      const d = await r.json();
      allow = !!d.allow;
      myOverrides = (d && d.overrides && typeof d.overrides === 'object') ? d.overrides : {};
    }
  } catch (_) { /* leave disabled on error */ }
  if (!allow) { slotEl.remove(); return; }

  // 2) Seed the editor from the LIVE effective theme (global + this user's
  // overrides already merged by ui-config into WA_APPEARANCE) + the live
  // background choice, then load the background catalog.
  let values = {};
  try {
    if (window.WA_APPEARANCE && typeof window.WA_APPEARANCE.getOverrides === 'function') {
      values = window.WA_APPEARANCE.getOverrides() || {};
    }
  } catch (_) { values = {}; }
  let bgChoice = { dark: 'stargaze', light: 'bullet-grid' };
  try {
    if (window.WA_BG && typeof window.WA_BG.getChoice === 'function') {
      const c = window.WA_BG.getChoice();
      if (c && c.dark) bgChoice = { dark: c.dark, light: c.light };
    }
  } catch (_) { /* keep defaults */ }
  let bgCatalog = [];
  try {
    const cr = await fetch(BG_API, { headers: _authHeaders() });
    if (cr.ok) { const d = await cr.json(); bgCatalog = Array.isArray(d.backgrounds) ? d.backgrounds : []; }
  } catch (_) { /* no background select options beyond None */ }

  // ── Scoped helpers (everything lives inside slotEl) ──
  const q = (id) => slotEl.querySelector('#' + id);

  const preview = (patch) => {
    try {
      if (window.WA_APPEARANCE && typeof window.WA_APPEARANCE.setOverrides === 'function') {
        window.WA_APPEARANCE.setOverrides(patch);
      }
    } catch (_) { /* preview best-effort */ }
  };

  async function saveMine(patch, ctrl) {
    Object.assign(values, patch);
    if (ctrl) _markSaving(ctrl);
    try {
      const res = await fetch(API, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ..._authHeaders() },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      try { const d = await res.json(); if (d && d.overrides) myOverrides = d.overrides; } catch (_) {}
      if (ctrl) _flashSaveCheck(ctrl, true);
    } catch (e) {
      if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    }
  }

  const tokenOrder = () => (window.WA_APPEARANCE && Array.isArray(window.WA_APPEARANCE.tokens))
    ? window.WA_APPEARANCE.tokens
    : Object.keys(TOKEN_META);

  // The colour a `follow` token actually shows when blank: the LIVE Panel swatch
  // at the theme's bubble alpha (mirrors index.css color-mix on --bg-elev).
  function followDefault(theme) {
    const sw = q(`me-sw-${theme}-surface_panel`);
    const panelHex = sw ? sw.value : asHex(values[keyFor('surface_panel', theme)], APP_DEFAULTS[theme].surface_panel);
    return combineColor(panelHex, FOLLOW_ALPHA[theme]);
  }
  function effectiveDefault(base, theme) {
    const meta = TOKEN_META[base];
    if (meta && meta.follow === 'panel') return followDefault(theme);
    if (meta && meta.follow === 'border') return APP_DEFAULTS[theme].border_color;
    return APP_DEFAULTS[theme][base] || '#000000';
  }
  function syncFollowSwatches(theme) {
    tokenOrder().forEach((base) => {
      const meta = TOKEN_META[base];
      if (!meta || !meta.follow) return;
      if (values[keyFor(base, theme)]) return;   // pinned → leave alone
      const split = splitColor(effectiveDefault(base, theme), '#000000');
      const sw = q(`me-sw-${theme}-${base}`);
      if (sw) sw.value = split.hex;
      const al = q(`me-al-${theme}-${base}`);
      if (al) al.value = split.alpha;
    });
  }

  function refreshPresetActive(theme, accentHex) {
    const host = q('me-presets-' + theme);
    if (!host) return;
    const cur = String(accentHex || '').toLowerCase();
    host.querySelectorAll('.ac-preset-chip').forEach((chip) => {
      const p = PRESETS.find((x) => x.id === chip.dataset.preset);
      if (!p) return;
      const presetAccent = asHex((p[theme] && p[theme].accent) || APP_DEFAULTS[theme].accent, '');
      chip.classList.toggle('active', presetAccent.toLowerCase() === cur);
    });
  }

  // Apply a preset to one mode — a COMPLETE RESET of every palette token for that
  // mode (mirror of the admin _applyPreset). Follow tokens reset to BLANK (track
  // the theme); the server's merge treats a blank value as "clear this token".
  function applyPreset(theme, preset) {
    const pv = (preset && preset[theme]) || {};
    const patch = {};
    tokenOrder().forEach((base) => {
      const meta = TOKEN_META[base];
      if (!meta) return;
      const key = keyFor(base, theme);
      let v;
      if (Object.prototype.hasOwnProperty.call(pv, base)) v = pv[base];
      else if (meta.follow) v = '';
      else v = APP_DEFAULTS[theme][base] || '';
      patch[key] = v;
      const disp = v || effectiveDefault(base, theme);
      const split = splitColor(disp, asHex(disp, '#000000'));
      const sw = q(`me-sw-${theme}-${base}`);
      if (sw) sw.value = split.hex;
      const al = q(`me-al-${theme}-${base}`);
      if (al) al.value = split.alpha;
    });
    preview(patch);
    saveMine(patch, q('me-presets-' + theme));
    refreshPresetActive(theme, pv.accent || APP_DEFAULTS[theme].accent);
  }

  function populateBgSelect(sel, current) {
    if (!sel) return;
    sel.innerHTML = '';
    const none = document.createElement('option');
    none.value = 'none';
    none.textContent = 'None — plain background';
    sel.appendChild(none);
    bgCatalog.forEach((bg) => {
      const o = document.createElement('option');
      o.value = bg.id;
      o.textContent = bg.name + (bg.status && bg.status !== 'stable' ? ` (${bg.status})` : '');
      sel.appendChild(o);
    });
    const known = current === 'none' || bgCatalog.some((b) => b.id === current);
    sel.value = known ? current : 'none';
  }

  function saveBackgroundChoice(theme, ctrl) {
    const sel = q('me-bg-' + theme);
    if (!sel) return;
    bgChoice[theme] = sel.value;
    try {
      if (window.WA_BG && typeof window.WA_BG.setChoice === 'function') {
        window.WA_BG.setChoice({ dark: bgChoice.dark, light: bgChoice.light });
      }
    } catch (_) {}
    saveMine({ [`background_${theme}`]: sel.value }, ctrl);
  }

  // Build one theme row's expanded body: preset chips + background select + a
  // colour swatch per palette token (mirror of the admin _buildThemeCard, minus
  // the admin-only "Add theme" button).
  function buildThemeBody(theme) {
    const body = q('me-theme-body-' + theme);
    if (!body) return;
    body.innerHTML = '';

    const presetsHost = document.createElement('div');
    presetsHost.className = 'ac-theme-presets';
    presetsHost.id = 'me-presets-' + theme;
    body.appendChild(presetsHost);
    PRESETS.forEach((p) => {
      const merged = { ...APP_DEFAULTS[theme], ...(p[theme] || {}) };
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ac-preset-chip';
      chip.dataset.preset = p.id;
      chip.innerHTML = `<span class="ac-preset-dot" style="background:${_esc(asHex(merged.accent, '#888'))};"></span>${_esc(p.name)}`;
      chip.addEventListener('click', () => applyPreset(theme, p));
      presetsHost.appendChild(chip);
    });

    // Background select row.
    const bgRow = document.createElement('div');
    bgRow.className = 'ac-theme-row';
    bgRow.innerHTML =
      '<span class="ac-theme-row-label"><i data-lucide="image" class="lucide-icon" style="width:15px;height:15px;color:var(--fg-3);"></i>Background</span>' +
      '<span class="ac-theme-row-ctl">' +
        `<select id="me-bg-${theme}" class="ac-input"></select>` +
      '</span>';
    body.appendChild(bgRow);
    const bgSel = bgRow.querySelector('select');
    populateBgSelect(bgSel, bgChoice[theme]);
    bgSel.addEventListener('change', () => saveBackgroundChoice(theme, bgRow.querySelector('.ac-theme-row-ctl')));

    // Colour swatch rows — order from WA_APPEARANCE.tokens (single source of truth).
    tokenOrder().forEach((base) => {
      const meta = TOKEN_META[base];
      if (!meta) return;
      const key = keyFor(base, theme);
      const fallback = effectiveDefault(base, theme);
      const split = splitColor(values[key] || fallback, asHex(fallback, '#000000'));
      const row = document.createElement('div');
      row.className = 'ac-theme-row';
      const alphaCtl = meta.alpha
        ? `<input type="range" class="ac-swatch-alpha" id="me-al-${theme}-${base}" min="0" max="100" step="1" value="${split.alpha}" title="Opacity" aria-label="${_esc(meta.label)} opacity">`
        : '';
      row.innerHTML =
        `<span class="ac-theme-row-label"><i data-lucide="${_esc(meta.icon)}" class="lucide-icon" style="width:15px;height:15px;color:var(--fg-3);"></i>${_esc(meta.label)}</span>` +
        '<span class="ac-theme-row-ctl">' +
          alphaCtl +
          `<input type="color" class="ac-swatch" id="me-sw-${theme}-${base}" value="${_esc(split.hex)}">` +
        '</span>';
      body.appendChild(row);

      const input = row.querySelector('input[type="color"]');
      const alpha = row.querySelector('input[type="range"]');
      const ctl = row.querySelector('.ac-theme-row-ctl');
      const curVal = () => (alpha ? combineColor(input.value, alpha.value) : input.value);
      const onPreview = () => {
        preview({ [key]: curVal() });
        if (base === 'accent') refreshPresetActive(theme, input.value);
        if (base === 'surface_panel' || base === 'border_color') syncFollowSwatches(theme);
      };
      const commit = () => {
        const v = curVal();
        preview({ [key]: v });
        saveMine({ [key]: v }, ctl);
        if (base === 'accent') refreshPresetActive(theme, input.value);
        if (base === 'surface_panel') syncFollowSwatches(theme);
      };
      input.addEventListener('input', onPreview);
      input.addEventListener('change', commit);
      if (alpha) {
        alpha.addEventListener('input', onPreview);
        alpha.addEventListener('change', commit);
      }
    });

    if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
  }

  function wireSelect(id, key) {
    const el = q(id);
    if (!el) return;
    el.addEventListener('change', () => {
      preview({ [key]: el.value });
      saveMine({ [key]: el.value }, el.closest('.ac-config-control'));
    });
  }

  async function resetMine(btn, statusEl) {
    if (btn) _markSaving(btn);
    try {
      const res = await fetch(API, { method: 'DELETE', headers: _authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (btn) _flashSaveCheck(btn, true);
      if (statusEl) { statusEl.textContent = 'Reset — reloading to apply the app theme…'; statusEl.style.display = 'block'; statusEl.style.color = 'var(--fg-2)'; }
      // Reload so ui-config re-seeds the (now global) theme into WA_APPEARANCE.
      setTimeout(() => window.location.reload(), 600);
    } catch (e) {
      if (btn) _flashSaveCheck(btn, false, e.message);
    }
  }

  // 3) Build the section shell, then fill the bodies + wire controls.
  const borderCur = values.border_width || '1px';
  const sansCur = values.font_sans || _SANS_OPTS[0][0];
  const monoCur = values.font_mono || _MONO_OPTS[0][0];
  slotEl.innerHTML = `
    <h3 class="account-section-title">Appearance</h3>
    <p style="font-size:13px;color:var(--fg-3);margin:0 0 14px;line-height:1.5;">
      Personalise your colours, background and fonts. Your choices apply to your account on every device and layer on top of the app's theme — anything you don't change still follows it.
    </p>
    <div class="ac-list" id="me-appearance-list">
      <div class="ac-row" id="me-theme-row-light" data-theme="light">
        <div class="ac-ability-row">
          <span class="ac-ability-icon"><i data-lucide="sun" class="lucide-icon" style="width:18px;height:18px;"></i></span>
          <div class="ac-ability-label">
            <div class="ac-ability-name">Light mode</div>
            <div class="ac-ability-desc">Preset themes, the animated background and the full colour palette for light mode.</div>
          </div>
          <span class="ac-row-chevron">${_CHEVRON}</span>
        </div>
        <div class="ac-ability-body" id="me-theme-body-light"></div>
      </div>
      <div class="ac-row" id="me-theme-row-dark" data-theme="dark">
        <div class="ac-ability-row">
          <span class="ac-ability-icon"><i data-lucide="moon" class="lucide-icon" style="width:18px;height:18px;"></i></span>
          <div class="ac-ability-label">
            <div class="ac-ability-name">Dark mode</div>
            <div class="ac-ability-desc">Preset themes, the animated background and the full colour palette for dark mode.</div>
          </div>
          <span class="ac-row-chevron">${_CHEVRON}</span>
        </div>
        <div class="ac-ability-body" id="me-theme-body-dark"></div>
      </div>
      <div class="ac-ability-row ac-row-static" id="me-border-row">
        <span class="ac-ability-icon"><i data-lucide="square-dashed" class="lucide-icon" style="width:18px;height:18px;"></i></span>
        <div class="ac-ability-label">
          <div class="ac-ability-name">Border thickness</div>
          <div class="ac-ability-desc">Outline weight for cards, tables and dividers across both themes. Off removes every border.</div>
        </div>
        <span class="ac-config-control">
          <select id="me-border-width" class="ac-input ac-config-sel">${_optionsHtml(_BORDER_OPTS, borderCur)}</select>
        </span>
      </div>
      <div class="ac-ability-row ac-row-static" id="me-font-sans-row">
        <span class="ac-ability-icon"><i data-lucide="type" class="lucide-icon" style="width:18px;height:18px;"></i></span>
        <div class="ac-ability-label">
          <div class="ac-ability-name">App font</div>
          <div class="ac-ability-desc">Typeface for all interface text, in both light and dark.</div>
        </div>
        <span class="ac-config-control">
          <select id="me-font-sans" class="ac-input ac-config-sel">${_optionsHtml(_SANS_OPTS, sansCur)}</select>
        </span>
      </div>
      <div class="ac-ability-row ac-row-static" id="me-font-mono-row">
        <span class="ac-ability-icon"><i data-lucide="code" class="lucide-icon" style="width:18px;height:18px;"></i></span>
        <div class="ac-ability-label">
          <div class="ac-ability-name">Code font</div>
          <div class="ac-ability-desc">Monospace typeface for code blocks, the terminal and logs.</div>
        </div>
        <span class="ac-config-control">
          <select id="me-font-mono" class="ac-input ac-config-sel">${_optionsHtml(_MONO_OPTS, monoCur)}</select>
        </span>
      </div>
    </div>
    <div class="account-row" style="margin-top:14px;align-items:center;gap:10px;">
      <button id="me-appearance-reset" class="account-btn">Reset my theme</button>
      <span id="me-appearance-reset-status" class="account-status" style="display:none;"></span>
    </div>
  `;

  // Fill the theme bodies + wire expand/collapse + shared selects + reset.
  buildThemeBody('light');
  buildThemeBody('dark');
  refreshPresetActive('light', asHex(values.accent_light, APP_DEFAULTS.light.accent));
  refreshPresetActive('dark', asHex(values.accent_dark, APP_DEFAULTS.dark.accent));

  ['light', 'dark'].forEach((theme) => {
    const row = q('me-theme-row-' + theme);
    const head = row && row.querySelector(':scope > .ac-ability-row');
    if (head) head.addEventListener('click', (e) => {
      if (e.target.closest('select, input, button, a, label')) return;
      row.classList.toggle('expanded');
    });
  });

  wireSelect('me-border-width', 'border_width');
  wireSelect('me-font-sans', 'font_sans');
  wireSelect('me-font-mono', 'font_mono');

  const resetBtn = q('me-appearance-reset');
  if (resetBtn) resetBtn.addEventListener('click', () => resetMine(resetBtn, q('me-appearance-reset-status')));

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

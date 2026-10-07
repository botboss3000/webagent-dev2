'use strict';
// SISTER-PANEL: AGENT-ABILITY-TABLE. Actual v2 dom-utils row/menu builders.
// Keep these copies synchronized with ui/shared/js/dom-utils.js; v3 saves are supplied as callbacks.
// Credential and tool-editor workflows remain unported; callers do not enable their options.
import { _markSaving, _flashSaveCheck } from './models/dom-utils.js';
const _noop = () => {};
const _TOOL_ALWAYS_ON = new Set(); // v3 authority is provided by the Rust projection, not v2 core names.
function _moreDotsSvg() {
  return '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';
}

export function _buildMoreButton(items, opts = {}) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ac-row-more';
  const title = opts.title || 'More options';
  btn.title = title;
  btn.setAttribute('aria-label', title);
  btn.innerHTML = _moreDotsSvg();
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (btn.disabled) return;
    const owner = btn._floatingMenuOwner || btn;
    if (document.getElementById('files-floating-menu')?._owner === owner) { closeFloatingMenu(); return; }
    const list = typeof items === 'function' ? items() : items;
    if (!list || !list.length) return;
    const point = btn._floatingMenuAnchorPoint;
    if (point && Number.isFinite(point.x) && Number.isFinite(point.y)) {
      // A hidden proxy button can forward its caller's pointer position so the
      // body-level menu still opens beside the control the user clicked.
      btn._floatingMenuAnchorPoint = null;
      openFloatingMenu(list, point.y + 8, point.x + 8, owner);
      return;
    }
    const r = btn.getBoundingClientRect();
    // Anchor the menu's right edge at the button's right edge (menu width 180).
    openFloatingMenu(list, r.bottom + 4, r.right - 180, owner);
  });
  return btn;
}

export function _buildPermissionTri(cfg) {
  const { value = 'auto', canEdit = true, onChange = _noop, ceiling = 'auto', displayOnly = false } = cfg || {};
  const ORDER = ['deny', 'ask', 'auto'];
  // Strictness rank — higher = stricter. The agent's pick must rank >= the
  // ceiling's rank (i.e. be at least as strict as the admin's global limit).
  const RANK = { auto: 0, ask: 1, deny: 2 };
  const ceilRank = RANK[ceiling] ?? 0;
  // Map the permission mode onto the group tri's off/mixed/on detents so the
  // knob position + colour are shared 1:1: deny→off (left, grey), ask→mixed
  // (middle, amber), auto→on (right, accent on a filled track).
  const _toState = m => (m === 'auto' ? 'on' : (m === 'ask' ? 'mixed' : 'off'));

  const tri = document.createElement('span');
  tri.className = 'ac-tri ac-tri-perm';
  tri.setAttribute('role', 'slider');
  tri.setAttribute('tabindex', canEdit ? '0' : '-1');
  tri.innerHTML = '<span class="ac-tri-knob"></span>';
  // Carry the ceiling on the element as an introspection hook (the old app3.css
  // hatch that consumed it is gone — the panel's greyed row shows the limit now).
  if (ceilRank > 0) tri.dataset.ceiling = ceiling;

  let current = ORDER.includes(value) ? value : 'auto';
  // Never display a value looser than the admin ceiling.
  if ((RANK[current] ?? 0) < ceilRank) current = ceiling;
  const _ceilNote = ceilRank > 0
    ? ` — admin global limit: ${ceiling === 'deny' ? 'Deny (locked)' : 'Ask'}, agents can only be stricter`
    : '';
  const _titleFor = m =>
    'Deny · Ask · Auto — '
    + (m === 'deny' ? 'blocked: the agent never sees this tool'
      : m === 'ask' ? 'allowed, but asks for confirmation before each use'
      : 'allowed, runs automatically')
    + _ceilNote;
  const paint = () => {
    tri.dataset.state = _toState(current);
    tri.title = _titleFor(current);
    tri.setAttribute('aria-valuetext', current);
  };
  paint();

  tri._mode = () => current;
  // Repaint from a new mode WITHOUT saving — a display-only caller (the tool row)
  // drives this from the tool's own state, never from clicks on the tri.
  tri._paint = (m) => { current = ORDER.includes(m) ? m : 'auto'; paint(); };

  // DISPLAY-ONLY — the tri renders the three detents (Deny · Ask · Auto) but OWNS
  // no selection: the tool row's choice is made in the panel this graphic opens,
  // so it must not swallow the click with its own detent zones. The row repaints
  // it from the tool's live permission via `_paint()`.
  if (displayOnly) {
    tri.classList.add('ac-tri-readonly');
    tri.setAttribute('role', 'button');
    tri.setAttribute('aria-haspopup', 'menu');
    tri.removeAttribute('aria-valuetext');
    tri.setAttribute('tabindex', canEdit ? '0' : '-1');
    return tri;
  }

  if (!canEdit) {
    tri.classList.add('ac-tri-disabled');
    tri.setAttribute('aria-disabled', 'true');
    tri.removeAttribute('tabindex');
    return tri;
  }

  let _busy = false;
  async function _go(next) {
    if (_busy || !ORDER.includes(next)) return;
    // Clamp a looser-than-ceiling request UP to the admin ceiling.
    if ((RANK[next] ?? 0) < ceilRank) next = ceiling;
    if (next === current) return;
    const prev = current;
    _busy = true;
    current = next;
    paint();                       // optimistic
    _markSaving(tri);
    let ok = true;
    try {
      const r = onChange(next, prev);
      if (r && typeof r.then === 'function') ok = (await r) !== false;
    } catch (_) { ok = false; }
    if (!ok) { current = prev; paint(); }   // revert — the save didn't land
    _flashSaveCheck(tri, ok);
    _busy = false;
  }
  tri._apply = _go;

  // Click zone → detent (left / middle / right third).
  tri.addEventListener('click', e => {
    e.stopPropagation();
    const r = tri.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;
    _go(frac < 1 / 3 ? 'deny' : (frac < 2 / 3 ? 'ask' : 'auto'));
  });
  // Keyboard: ←/↓ step toward Deny, →/↑ step toward Auto, Home/End jump to ends.
  tri.addEventListener('keydown', e => {
    const i = ORDER.indexOf(current);
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); _go(ORDER[Math.min(ORDER.length - 1, i + 1)]); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); _go(ORDER[Math.max(0, i - 1)]); }
    else if (e.key === 'Home') { e.preventDefault(); _go('deny'); }
    else if (e.key === 'End') { e.preventDefault(); _go('auto'); }
  });

  return tri;
}

export function _refreshLucideIcons(host) {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      const scope = host || document;
      const nodes = Array.from(scope.querySelectorAll('[data-lucide]:not(.lucide)'));
      if (!nodes.length) return;
      window.lucide.createIcons({ nodes });
      // The vendored lucide copies data-lucide onto the svgs it renders (and
      // renders them document-wide regardless of `nodes`). Remove the marker so
      // those svgs never match a future [data-lucide] scan again.
      scope.querySelectorAll('svg[data-lucide].lucide').forEach(function (svg) {
        svg.removeAttribute('data-lucide');
      });
    } catch (_) {}
  }
}

export function buildConfigRow(opts = {}) {
  const row = document.createElement('div');
  row.className = 'ac-ability-row ac-config-row' + (opts.className ? (' ' + opts.className) : '');
  if (opts.name != null || opts.desc) {
    const label = document.createElement('span');
    label.className = 'ac-ability-label';
    if (opts.name != null) {
      const nameEl = document.createElement('span');
      nameEl.className = 'ac-ability-name' + (opts.nameClass ? (' ' + opts.nameClass) : '');
      nameEl.textContent = String(opts.name);
      nameEl.title = String(opts.name);
      label.appendChild(nameEl);
    }
    if (opts.desc) {
      const descEl = document.createElement('span');
      descEl.className = 'ac-ability-desc';
      descEl.textContent = String(opts.desc);
      // Clamped to one line by default, so keep the full text reachable on hover
      // (the same treatment `_buildToolRow` gives a tool description).
      if (opts.descTitle) descEl.title = opts.descTitle;
      label.appendChild(descEl);
    }
    row.appendChild(label);
  }
  const ctrl = document.createElement('span');
  ctrl.className = 'ac-config-control';
  if (opts.control) ctrl.appendChild(opts.control);
  row.appendChild(ctrl);
  return row;
}

export function _buildToolRow(toolMeta, opts) {
  const { canEdit = false, showPerm = false } = opts || {};
  const onPermissionChange = (opts && opts.onPermissionChange) || _noop;
  const onVisibilityChange = (opts && opts.onVisibilityChange) || (async () => {});

  const row = document.createElement('div');
  row.className = 'ac-ability-row ac-tool-row';

  // Name (+ description) — promoted to flex children of the row by the shared
  // `.ac-ability-label` (display:contents), so the name sits on the title line
  // and the description drops to its own full-width line below it.
  const label = document.createElement('span');
  label.className = 'ac-ability-label';
  // A skill editor is available only when this exact tool has a current Markdown
  // association. The App caller also uses the same association for its purely
  // informational missing-skill chip; per-agent callers leave it disabled.
  const hasToolSkill = opts && opts.hasToolSkill === true;
  const canOpenSkill = !!(opts && opts.abilityId && hasToolSkill && typeof opts.onOpenToolSkill === 'function');
  const nameEl = document.createElement(canOpenSkill ? 'button' : 'span');
  nameEl.className = 'ac-ability-name ac-tool-name' + (canOpenSkill ? ' ac-tool-skill-link' : '');
  // Only the per-agent V3 catalog supplies callCount. Other shared row callers
  // retain their existing labels.
  const count = Number.isSafeInteger(toolMeta.callCount) && toolMeta.callCount >= 0
    ? ` (${toolMeta.callCount})` : '';
  nameEl.textContent = toolMeta.name + count;
  nameEl.title = count
    ? `${toolMeta.name}: ${toolMeta.callCount} recorded calls in your sessions with this agent`
    : (canOpenSkill ? `Open ${toolMeta.name} tool skill` : toolMeta.name);
  if (canOpenSkill) {
    nameEl.type = 'button';
    nameEl.addEventListener('click', (event) => {
      event.stopPropagation();
      void opts.onOpenToolSkill(toolMeta.name);
    });
  }
  // The semantic header keeps the name and optional App-only chip as siblings
  // while `display: contents` preserves the shared stacked-row flex layout.
  const header = document.createElement('span');
  header.className = 'ac-tool-header';
  header.appendChild(nameEl);
  if (opts && opts.showMissingSkill === true && !hasToolSkill) {
    const chip = document.createElement('span');
    chip.className = 'ac-tool-missing-skill-chip';
    chip.textContent = 'No skill';
    chip.title = 'No dedicated Markdown skill is associated with this tool. Add one only when operational guidance would be useful.';
    header.appendChild(chip);
  }
  label.appendChild(header);
  if (toolMeta.description) {
    // The description line is a separate, full-width flex item. The optional
    // App-admin edit button is its sibling, so it stays at the end of the text
    // line without becoming part of the clamped/click-to-expand description.
    const descLine = document.createElement('span');
    descLine.className = 'ac-tool-description-line';
    const descEl = document.createElement('span');
    descEl.className = 'ac-ability-desc ac-tool-description';
    descEl.textContent = toolMeta.description;
    descEl.title = toolMeta.description;
    descLine.appendChild(descEl);
    if (opts && opts.toolDescriptionEditable === true && opts.abilityId) {
      const editButton = document.createElement('button');
      editButton.type = 'button';
      editButton.className = 'ac-tool-description-edit';
      editButton.textContent = 'Edit';
      editButton.title = `Edit description for ${toolMeta.name}`;
      editButton.setAttribute('aria-label', `Edit description for ${toolMeta.name}`);
      editButton.addEventListener('click', (event) => {
        event.stopPropagation();
        void openToolDescriptionEditor(opts.abilityId, toolMeta.name, toolMeta.description, (updated) => {
          toolMeta.description = updated;
          descEl.textContent = updated;
          descEl.title = updated;
        });
      });
      descLine.appendChild(editButton);
    }
    label.appendChild(descLine);

    // Only a genuinely clamped description becomes interactive. This keeps
    // short descriptions inert while making the full canonical tool text
    // available with a click (or Enter/Space) on the text itself.
    requestAnimationFrame(() => {
      if (descEl.scrollHeight <= descEl.clientHeight + 1) return;
      descEl.classList.add('ac-tool-description-toggle');
      descEl.tabIndex = 0;
      descEl.setAttribute('role', 'button');
      descEl.setAttribute('aria-expanded', 'false');
      descEl.setAttribute('aria-label', `Expand description for ${toolMeta.name}`);
      const toggleDescription = () => {
        const expanded = row.classList.toggle('ac-tool-desc-expanded');
        descEl.setAttribute('aria-expanded', String(expanded));
        descEl.setAttribute(
          'aria-label',
          `${expanded ? 'Collapse' : 'Expand'} description for ${toolMeta.name}`,
        );
      };
      descEl.addEventListener('click', (event) => {
        event.stopPropagation();
        toggleDescription();
      });
      descEl.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        event.stopPropagation();
        toggleDescription();
      });
    });
  }
  row.appendChild(label);

  // The control slot — the SAME right-aligned `.ac-config-control` a config row
  // uses, so a tool's control and a setting's field/menu land on the same edge.
  const ctrl = document.createElement('span');
  ctrl.className = 'ac-config-control';
  row.appendChild(ctrl);

  // TOOL-STATE-PANEL: the row owns NO interactive toggle. Its only control is a
  // launcher that opens a panel holding the tool's WHOLE state — Permission
  // (Off · Ask · Auto) and, when this caller tunes discovery, Visibility
  // (Discoverable · Visible) — with exactly one selection per section. State is
  // only ever changed by a selection inside that panel, mirroring the ability
  // row's panel-driven selection.
  const permLocked = toolMeta.locked || _TOOL_ALWAYS_ON.has(toolMeta.name);
  if (showPerm && permLocked) {
    const pill = document.createElement('span');
    pill.className = 'agents-tool-always-on';
    pill.textContent = 'always on';
    pill.title = 'Essential tool — always permitted, cannot be gated.';
    ctrl.appendChild(pill);
  }

  // Core tools are always sent, so their state is fixed — no panel at all.
  if (toolMeta.locked) {
    const pill = document.createElement('span');
    pill.className = 'agents-tool-always-on';
    pill.textContent = 'core';
    pill.title = 'Core tool — always sent, cannot be changed.';
    ctrl.appendChild(pill);
    return row;
  }

  // Per-agent discovery only; the admin panel passes showVisibility:false (it
  // sets the ceiling — enable + permission — not discovery).
  const showVisibility = !(opts && opts.showVisibility === false);
  const usePerm = showPerm && !permLocked;
  if (usePerm || showVisibility) {
    ctrl.appendChild(_buildToolStateControl(toolMeta, {
      canEdit,
      showPerm: usePerm,
      showVisibility,
      onPermissionChange,
      onVisibilityChange,
    }));
  }
  return row;
}

const _TOOL_STATE_OFF_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></svg>';
const _TOOL_STATE_ASK_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.4 9.2a2.7 2.7 0 1 1 3.7 2.5c-.7.3-1.1.9-1.1 1.6v.3"/><path d="M12 17h.01"/></svg>';
const _TOOL_STATE_AUTO_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.1 12.6a1 1 0 0 0 .8 1.6h5.4l-.7 7.8 8.9-10.6a1 1 0 0 0-.8-1.6h-5.4z"/></svg>';

function _buildToolStateControl(toolMeta, opts) {
  const {
    canEdit = false,
    showPerm = true,
    showVisibility = true,
    onPermissionChange = _noop,
    onVisibilityChange = _noop,
  } = opts || {};

  const ORDER = ['deny', 'ask', 'auto'];          // least → most permissive
  const RANK = { auto: 0, ask: 1, deny: 2 };      // higher = stricter
  const ceiling = toolMeta.ceiling || 'auto';     // loosest allowed (admin limit)
  const ceilRank = RANK[ceiling] ?? 0;            // looser picks clamp UP to this

  let perm = ORDER.includes(toolMeta.permMode) ? toolMeta.permMode : 'auto';
  if ((RANK[perm] ?? 0) < ceilRank) perm = ceiling;
  let vis = toolMeta.mode === 'discoverable' ? 'discoverable' : 'visible';

  // ── The control — the shared 3-position PERMISSION tri (Off · Ask · Auto) ───
  // The tool's toggle IS the `.ac-tri` its row used before, so it reads as one
  // component with the ability tables' toggles. It is DISPLAY-ONLY: clicking it
  // opens the panel holding BOTH sections, and the choice is made there. The
  // knobs show the permission; the BORDER carries the visibility — neutral when
  // the tool has no visibility, green once it is discoverable or visible.
  const tri = _buildPermissionTri({
    value: perm,
    ceiling,
    canEdit,
    displayOnly: true,
  });
  tri.classList.add('ac-tool-perm-tri');

  const _permText = m => (m === 'deny' ? 'Off' : (m === 'ask' ? 'Ask' : 'Auto'));
  const _visText = m => (m === 'discoverable' ? 'Discoverable' : 'Visible');
  const _permSvg = m => (m === 'deny' ? _TOOL_STATE_OFF_SVG : (m === 'ask' ? _TOOL_STATE_ASK_SVG : _TOOL_STATE_AUTO_SVG));

  function paint() {
    tri._paint(perm);                                  // knobs = the permission
    // Border = the tool's VISIBILITY. An Off (deny) tool has none — it is neither
    // discoverable nor visible — so it reads neutral grey; Ask/Auto read green.
    tri.dataset.vis = (showVisibility && perm !== 'deny') ? vis : 'none';
    const tip = 'Permission: ' + _permText(perm)
      + (showVisibility ? ' · Visibility: ' + _visText(vis) : '')
      + ' — click to choose';
    tri.title = tip;
    tri.setAttribute('aria-label', tip);
  }
  paint();

  // ── Persist one section's pick. Optimistic, with revert on failure. ────────
  async function _save(kind, next) {
    const isPerm = kind === 'perm';
    const cur = isPerm ? perm : vis;
    if (isPerm && (RANK[next] ?? 0) < ceilRank) next = ceiling;   // clamp to ceiling
    if (next === cur) return true;
    if (isPerm) perm = next; else vis = next;
    paint();                                   // optimistic
    _markSaving(tri);
    let ok = true;
    let failureMessage = '';
    try {
      const r = isPerm
        ? onPermissionChange(toolMeta.name, next)
        : onVisibilityChange(toolMeta.name, next);
      // Same contract as before: resolve `false` or throw → revert.
      if (r && typeof r.then === 'function') ok = (await r) !== false;
    } catch (error) {
      ok = false;
      failureMessage = error?.message || 'Not saved';
    }
    if (!ok) { if (isPerm) perm = cur; else vis = cur; paint(); }
    _flashSaveCheck(tri, ok, failureMessage);
    return ok;
  }

  // ── The panel body. Rebuilt on every open/repaint from live state, so the
  //    ✓ marks always show exactly one selection per section. ─────────────────
  const _menuItems = () => {
    const out = [];
    if (showPerm) {
      out.push({ info: true, className: 'ac-tool-state-head', label: 'Permission' });
      for (const m of ORDER) {
        out.push({
          iconHtml: _permSvg(m),
          label: _permText(m),
          checked: perm === m,
          // A pick looser than the admin ceiling is not offered (same lock the
          // old tri-toggle showed on its locked detent).
          disabled: !canEdit || (RANK[m] ?? 0) < ceilRank,
          keepOpen: true,
          action: () => _save('perm', m),
        });
      }
    }
    if (showVisibility) {
      if (out.length) out.push({ separator: true });   // the hairline divider
      out.push({ info: true, className: 'ac-tool-state-head', label: 'Visibility' });
      for (const v of ['discoverable', 'visible']) {
        out.push({
          iconHtml: v === 'discoverable' ? _EYE_OFF_SVG : _EYE_ON_SVG,
          label: _visText(v),
          checked: vis === v,
          disabled: !canEdit,
          keepOpen: true,
          action: () => _save('vis', v),
        });
      }
    }
    return out;
  };

  tri.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); tri.click(); }
  });
  tri.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!canEdit) return;
    if (document.getElementById('files-floating-menu')?._owner === tri) { closeFloatingMenu(); return; }
    const r = tri.getBoundingClientRect();
    // Anchor the panel just under the toggle, right-edge aligned. 180 is the
    // width `openFloatingMenu` clamps against, so the two stay consistent, and
    // the click's own Y keeps the panel beside the detent the user hit.
    const top = e.clientY ? e.clientY + 8 : r.bottom + 4;
    openFloatingMenu(_menuItems, top, Math.max(8, r.right - 180), tri);
  });

  return tri;
}

const _EYE_ON_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const _EYE_OFF_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/></svg>';

let floatingMenuCleanup = null;

export function closeFloatingMenu() {
  if (floatingMenuCleanup) { floatingMenuCleanup(); floatingMenuCleanup = null; }
  const m = document.getElementById('files-floating-menu');
  if (m) m.remove();
}

export function openFloatingMenu(items, top, left, owner = null) {
  closeFloatingMenu();

  const menu = document.createElement('div');
  menu.className = 'files-tab-menu';
  menu.id = 'files-floating-menu';
  menu._owner = owner;

  // `items` may be a FACTORY rather than a plain array: a multi-section panel
  // (e.g. the tool Permission + Visibility picker) repaints itself in place
  // after each selection, so the factory is re-invoked on every open/repaint to
  // read the live values.
  const list = (typeof items === 'function' ? items() : items) || [];

  for (const item of list) {
    if (item.separator) {
      const hr = document.createElement('div');
      hr.className = 'files-tab-menu-sep';
      menu.appendChild(hr);
      continue;
    }
    if (item.info) {
      const row = document.createElement('div');
      // `className` lets a section header opt out of the info row's fixed
      // min-width (used by the tool-state panel's section labels).
      row.className = 'files-tab-menu-info' + (item.className ? ' ' + item.className : '');
      if (item.id) row.id = item.id;
      row.textContent = item.label || '';
      menu.appendChild(row);
      continue;
    }
    if (item.field) {
      const wrap = document.createElement('div');
      wrap.className = 'files-tab-menu-field';
      if (item.label) {
        const lab = document.createElement('label');
        lab.textContent = item.label;
        wrap.appendChild(lab);
      }
      const inp = document.createElement('input');
      inp.type = item.fieldType || 'text';
      inp.value = item.value || '';
      inp.placeholder = item.placeholder || '';
      inp.spellcheck = false;
      inp.autocomplete = item.fieldType === 'password' ? 'new-password' : 'off';
      if (item.fieldKey) inp.dataset.field = item.fieldKey;
      inp.addEventListener('click', (e) => e.stopPropagation());
      if (typeof item.onInput === 'function') {
        inp.addEventListener('input', () => item.onInput(inp.value));
      }
      if (typeof item.onSave === 'function') {
        inp.addEventListener('change', () => item.onSave(inp.value));
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); item.onSave(inp.value); inp.blur(); }
        });
      }
      wrap.appendChild(inp);
      menu.appendChild(wrap);
      continue;
    }
    const btn = document.createElement('button');
    btn.className = 'files-tab-menu-item' + (item.danger ? ' danger' : '') + (item.checked ? ' checked' : '');
    btn.type = 'button';
    btn.disabled = !!item.disabled;
    // An item may ship its own inline SVG (`iconHtml`) instead of a lucide name.
    // The ability tables' controls use inline SVGs so they never depend on a
    // lucide pass having run on the page.
    let i;
    if (item.iconHtml) {
      i = document.createElement('span');
      i.className = 'files-tab-menu-icon';
      i.setAttribute('aria-hidden', 'true');
      i.innerHTML = item.iconHtml;
    } else {
      i = document.createElement('i');
      i.setAttribute('data-lucide', item.icon || 'circle');
      i.className = 'lucide-icon';
    }
    btn.appendChild(i);
    const lbl = document.createElement('span');
    lbl.textContent = item.label;
    btn.appendChild(lbl);
    const check = document.createElement('span');
    check.className = 'files-tab-menu-check';
    check.textContent = '\u2713';
    btn.appendChild(check);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionable = !item.disabled && typeof item.action === 'function';
      // A `keepOpen` item belongs to a MULTI-SECTION panel: the user still has
      // another section to set, so the panel stays up and repaints in place (the
      // ✓ marks follow the new selection) instead of dismissing on the first pick.
      if (actionable && item.keepOpen) {
        const r = item.action();
        const repaint = () => {
          // Only repaint if the user hasn't already dismissed the panel.
          if (!document.getElementById('files-floating-menu')) return;
          openFloatingMenu(items, top, left, owner);
        };
        repaint();                                        // optimistic
        if (r && typeof r.then === 'function') r.then(repaint, repaint);   // settle/revert
        return;
      }
      closeFloatingMenu();
      if (actionable) item.action();
    });
    menu.appendChild(btn);
  }

  document.body.appendChild(menu);

  const menuWidth = 180;
  const rect = menu.getBoundingClientRect();
  const menuHeight = rect.height || 8;
  const clampedTop  = Math.max(8, Math.min(window.innerHeight - menuHeight - 8, top));
  const clampedLeft = Math.max(8, Math.min(window.innerWidth  - menuWidth  - 8, left));
  menu.style.top  = clampedTop + 'px';
  menu.style.left = clampedLeft + 'px';

  _refreshLucideIcons(menu);

  // The owning toggle handles repeat clicks; all other outside clicks dismiss.
  const outside = event => {
    const proxy = event.target.closest?.('.ac-row-more');
    if (menu.contains(event.target) || owner?.contains(event.target) ||
        (owner && proxy?._floatingMenuOwner === owner)) return;
    closeFloatingMenu();
  };
  const onKey = event => { if (event.key === 'Escape') closeFloatingMenu(); };
  const cleanup = () => {
    document.removeEventListener('click', outside, true);
    document.removeEventListener('contextmenu', outside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  floatingMenuCleanup = cleanup;
  document.addEventListener('click', outside, true);
  document.addEventListener('contextmenu', outside, true);
  document.addEventListener('keydown', onKey, true);
}

export function _buildAbilitySettingsList() {
  const list = document.createElement('div');
  list.className = 'ac-list ac-config-list ac-config-settings';
  return list;
}

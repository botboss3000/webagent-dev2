// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║  SISTER-PANEL: AGENT-ABILITY-TABLE  —  KEEP THE TWO IN SYNC                 ║
// ║  This is the **admin** ability table (global app-level controls).            ║
// ║  The **agent** ability table is `agent-ability-table.js`.                    ║
// ║  Both render the shared `.ac-list` / `.ac-row` / `.ac-tri` component and     ║
// ║  read as the SAME table. If you change the look, structure, grouping, or     ║
// ║  toggle behaviour here, apply the matching change in agent-ability-table.js. ║
// ║  (grep `SISTER-PANEL: AGENT-ABILITY-TABLE`).                                 ║
// ║                                                                              ║
// ║  The admin table is the SUPERSET — it has everything the agent table has     ║
// ║  plus admin-only features: delete buttons, integration groups, global        ║
// ║  credential configuration. The agent table omits these and adds nothing      ║
// ║  the admin table doesn't already have.                                       ║
// ╚══════════════════════════════════════════════════════════════════════════════╝
//
// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.
//
import {
  _esc, _iconHtml, _chevronSvg,
  _buildComingSoonPill, _buildToolRow, _buildAbilitySettingsList,
  _buildAbilityTreeLoader, _staggerRevealGroups, _buildSettingsRowsHtml,
  buildCredentialsSection, _flashStatusText, _markSaving, _flashSaveCheck,
  buildAbilitySearchPill, _buildMoreButton, _buildGroupStatusCard, _paintGroupStatus,
  loadAbilityCatalog, invalidateAbilityCatalog, openFloatingMenu,
} from './dom-utils.js';
import { authHeaders } from './left-login.js';
import { apiPath } from './config.js';
// ABILITY_TO_TOOLS imported DIRECTLY from the agents state module (NOT window.*,
// which is never assigned). This file sits in ui/shared/js/, so the module is at
// ../../main-panel/agents/js/state.js.
import { ABILITY_TO_TOOLS } from '../../main-panel/agents/js/state.js';

// ── Catalog fetch (shared with agent-ability-table.js — mirror changes) ───────
// The ability catalog (`GET /api/v1/abilities/catalog`) is the drop-in data
// source. A file in plugins/abilities/ shows up here with no JS edit. Loading +
// caching is shared with agent-ability-table.js via loadAbilityCatalog() /
// invalidateAbilityCatalog() in dom-utils.js (both panels previously kept a
// byte-identical copy of the loader + cache).

// ── Fallback data (shared with agent-ability-table.js — mirror changes) ───────
// These are the hard-coded defaults for when the catalog endpoint is unreachable.
// The catalog overwrites them at runtime. Do NOT add a new ability here; drop a
// file in plugins/abilities/ instead.

// ── Shared visual skeleton (mirror in agent-ability-table.js) ─────────────────
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SKELETON START — if you change any of these functions (group builder,  ║
// ║  row builder, tri-toggle, search, expand/collapse), mirror the change    ║
// ║  in agent-ability-table.js.  The CSS classes they use live in app3.css. ║
// ╚══════════════════════════════════════════════════════════════════════════╝

// Imported from shared/dom-utils.js: _esc, _iconHtml, _chevronSvg

/** Build one expandable group row (`.ac-row.ac-group`) with head + body. */
function _buildGroup(groupId, name, icon, color, desc) {
  const groupEl = document.createElement('div');
  groupEl.className = 'ac-row ac-group';
  groupEl.id = 'ac-group-' + groupId;

  const head = _buildGroupHead(groupId, name, icon, color, desc);
  const bodyEl = document.createElement('div');
  bodyEl.className = 'ac-group-body';
  bodyEl.id = 'ac-group-body-' + groupId;

  groupEl.appendChild(head);
  groupEl.appendChild(bodyEl);

  // Expand / collapse on head click — but not when the click lands on the tri
  // toggle or the group delete button.
  head.addEventListener('click', e => {
    if (e.target.closest('.ac-tri, button')) return;
    groupEl.classList.toggle('expanded');
  });

  return groupEl;
}

/** Build the always-visible group head row (chevron + icon + name + desc). The
 *  group toggle (tri) and delete button are appended by the caller, on the right. */
function _buildGroupHead(groupId, name, icon, color, desc) {
  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-group-head';

  // Expand chevron — LEFT of the icon (mirrors the member rows).
  const chevron = document.createElement('span');
  chevron.className = 'ac-row-chevron ac-row-chevron--left';
  chevron.innerHTML = _chevronSvg();
  head.appendChild(chevron);

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = color;
  iconWrap.innerHTML = _iconHtml(icon, '18px');

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = name;
  label.querySelector('.ac-ability-desc').textContent = desc;

  head.appendChild(iconWrap);
  head.appendChild(label);

  return head;
}

/** Append a read-only group status card to a group head (replaces the old
 *  interactive tri-toggle). State: 'off' → Disabled, 'mixed' → Partial,
 *  'on' → Enabled, 'na' → "—" (no togglable members). The card is display-only —
 *  bulk group toggling is intentionally gone. (SISTER-PANEL: AGENT-ABILITY-TABLE.) */
function _appendGroupStatus(head, statusId, state, title) {
  const status = _buildGroupStatusCard(statusId, state, title);
  head.appendChild(status);
  return status;
}

// Imported from shared/dom-utils.js: _buildTriToggle, _wireTriToggle

/** Build one always-visible member row (icon + name + desc + optional toggle). */
function _buildMemberRow(id, iconHtml, color, name, desc) {
  const row = document.createElement('div');
  row.className = 'ac-ability-row';
  row.id = 'ac-ability-row-' + id;
  row.dataset.ability = id;

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = color;
  iconWrap.innerHTML = iconHtml;

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = name;
  label.querySelector('.ac-ability-desc').textContent = desc;

  row.appendChild(iconWrap);
  row.appendChild(label);
  return row;
}

/** Append a toggle switch to the right side of a row. */
function _appendToggle(row, toggleId, checked, canEdit, titleOn, titleOff) {
  const toggleWrap = document.createElement('label');
  toggleWrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  toggleWrap.title = checked ? (titleOff || 'Disable') : (titleOn || 'Enable');

  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.className = 'conn-toggle';
  toggle.id = toggleId;
  toggle.checked = !!checked;
  if (!canEdit) toggle.disabled = true;

  const track = document.createElement('span');
  track.className = 'conn-toggle-track';

  toggleWrap.appendChild(toggle);
  toggleWrap.appendChild(track);

  // Status slot — left of the toggle, in line with the title. The save handler
  // types "Saved" / "Failed" into it, then it erases itself. (SISTER-PANEL.)
  const status = document.createElement('span');
  status.className = 'ac-ability-status';
  row.appendChild(status);
  row.appendChild(toggleWrap);

  return { toggle, toggleWrap, status };
}

// Imported from shared/dom-utils.js: _buildComingSoonPill

// ── Search filtering ──────────────────────────────────────────────────────────

// Live filter driven by the hybrid search/chat pill's `onFilter`. Same behaviour
// as the old search box. (SISTER-PANEL: mirrors _runAgentFilter in
// agent-ability-table.js.)
function _runAdminFilter(rawQuery, grid, groups) {
  const q = (rawQuery || '').trim().toLowerCase();

  // Score each row by its haystack text.
  groups.forEach(g => {
    let anyVisible = false;
    g.rows.forEach(r => {
      const match = !q || r.haystack.includes(q);
      r.el.style.display = match ? '' : 'none';
      if (match) anyVisible = true;
    });
    // Expand while searching so members are reachable.
    g.groupEl.classList.toggle('expanded', !!q);
    if (!q) { g.groupEl.style.display = ''; return; }
    g.groupEl.style.display = anyVisible ? '' : 'none';
  });
}

// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SKELETON END                                                           ║
// ╚══════════════════════════════════════════════════════════════════════════╝

// ── Admin-specific: ability state management ──────────────────────────────────

let _abilityStates = {};            // { [abilityId]: boolean }
let _abilityLastActionAt = {};      // { [abilityId]: DOMHighResTimeStamp }

function _markAbilityAction(ability) {
  _abilityLastActionAt[ability] = (typeof performance !== 'undefined' ? performance.now() : Date.now());
}

function _applyAbilityRowState(ability, enabled) {
  _abilityStates[ability] = enabled;
  const toggle = document.getElementById('ac-ability-toggle-' + ability);
  if (toggle) toggle.checked = !!enabled;
  const row = document.getElementById('ac-ability-row-' + ability);
  if (row) row.classList.toggle('ac-ability-enabled', !!enabled);
}

// ── Admin-specific: ability toggle save ───────────────────────────────────────

/** The per-ability status slot — kept for plain text messages ("Cannot be
 *  deactivated"); save outcomes now overlay the toggle (see `_abilityToggleWrap`). */
function _abilityStatusEl(ability) {
  const row = document.getElementById('ac-ability-row-' + ability);
  return row ? row.querySelector('.ac-ability-status') : null;
}

/** The per-ability toggle wrap — the save spinner → ✓ / ⚠ overlays ON TOP of it
 *  (mirrors the agent panel; SISTER-PANEL: AGENT-ABILITY-TABLE). */
function _abilityToggleWrap(ability) {
  const tgl = document.getElementById('ac-ability-toggle-' + ability);
  return tgl ? tgl.closest('.ac-ability-toggle-wrap') : null;
}

// Persist one ability toggle. Returns true on a confirmed save, false on a
// failure OR a user cancellation (e.g. declining the Automation purge). The
// CALLER owns the toggle's visual position — this function never moves the
// checkbox; it only performs the network call, updates the shared state on
// success, and flashes the check/cross confirmation on the row's delete button.
// See _runAbilityToggle for the spinner → confirm → settle sequence.
async function _saveAbility(ability, enabled) {
  try {
    if (enabled) {
      const res = await _apiFetch('/admin/integrations/abilities/' + ability, { method: 'POST' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
    } else {
      if (ability === 'automation') {
        const ok = confirm(
          'Disable Automation?\n\n'
          + 'This will permanently delete every agent\'s scheduled tasks and event subscriptions. '
          + 'This cannot be undone.'
        );
        if (!ok) return false;  // cancelled — caller keeps the prior position
      }
      const res = await _apiFetch('/admin/integrations/abilities/' + ability, { method: 'DELETE' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
    }
    _markAbilityAction(ability);
    _abilityStates[ability] = enabled;
    _applyAbilityRowState(ability, enabled);
    // Confirm with the shared check overlaid ON TOP of the toggle (the same
    // indicator the agent panel uses), so both sister panels read one language.
    _flashSaveCheck(_abilityToggleWrap(ability), true);
    return true;
  } catch (e) {
    // The caller reverts the toggle to its prior position; flash the warning
    // triangle over the toggle to match every other config control.
    _flashSaveCheck(_abilityToggleWrap(ability), false);
    return false;
  }
}

// Lock the toggle and overlay the in-flight spinner ON TOP of it while saving.
function _setToggleSaving(toggleEl, saving) {
  if (!toggleEl) return;
  toggleEl.disabled = !!saving;
  if (saving) _markSaving(toggleEl.closest('.ac-ability-toggle-wrap'));
}

// Re-sync the parent group's status card (Disabled · Partial · Enabled) from the
// current member states after one member changed.
function _resyncParentStatus(toggleEl) {
  const groupEl = toggleEl.closest('.ac-group');
  if (!groupEl) return;
  const status = groupEl.querySelector('.ac-group-status');
  if (!status) return;
  const body = groupEl.querySelector('.ac-group-body');
  const rows = body ? [...body.querySelectorAll('[data-ability]')] : [];
  const onCount = rows.filter(r => !!_abilityStates[r.dataset.ability]).length;
  status.dataset.state = onCount === 0 ? 'off' : (onCount === rows.length ? 'on' : 'mixed');
  _paintGroupStatus(status);
}

// The full single-toggle UX: hold the toggle at its prior position, show a
// spinner while saving, then settle it into `desired` only after the save
// confirms (the row's delete button flashes a check). On failure/cancel it
// stays at the prior position (a cross flashes on failure).
async function _runAbilityToggle(toggleEl, id, desired) {
  _markAbilityAction(id);
  toggleEl.checked = !desired;          // hold prior position until confirmed
  _setToggleSaving(toggleEl, true);
  const ok = await _saveAbility(id, desired);
  _setToggleSaving(toggleEl, false);
  if (ok) toggleEl.checked = desired;   // settle into the new position
  _resyncParentStatus(toggleEl);
  return ok;
}

// ── Admin-specific: long-press delete button ──────────────────────────────────

// The delete button's normal trash icon. (It no longer doubles as a save
// indicator — saves now confirm in the row's shared `.ac-ability-status` slot.)
const _DEL_TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';

const _DEL_REST_COLOR = 'rgba(var(--danger-rgb),0.5)';
const _DEL_REST_BORDER = 'rgba(var(--danger-rgb),0.25)';

// Generic hold-to-delete button. `onConfirm(delBtn)` fires after a 1.5s hold.
// Used for both per-ability deletes and whole-group deletes so the two buttons
// look and behave identically and line up on the same right edge.
function _buildHoldDeleteButton(onConfirm, holdTitle) {
  const delBtn = document.createElement('button');
  delBtn.className = 'ac-ability-delete-btn';
  delBtn.type = 'button';
  delBtn.title = holdTitle || 'Hold 1.5s to delete';
  delBtn.style.cssText = 'background:none;border:none;border-radius:6px;color:' + _DEL_REST_COLOR + ';cursor:pointer;padding:4px 6px;margin-right:4px;transition:all 0.3s;position:relative;overflow:hidden;flex-shrink:0;';

  const iconSpan = document.createElement('span');
  iconSpan.className = 'ac-del-icon';
  iconSpan.style.cssText = 'position:relative;z-index:1;display:inline-flex;align-items:center;justify-content:center;';
  iconSpan.innerHTML = _DEL_TRASH_SVG;
  delBtn.appendChild(iconSpan);

  const progress = document.createElement('span');
  progress.className = 'ac-del-progress';
  progress.style.cssText = 'position:absolute;inset:0;background:rgba(var(--danger-rgb),0.15);width:0%;border-radius:5px;transition:width 0.1s linear;pointer-events:none;';
  delBtn.appendChild(progress);

  let _holdTimer = null;
  let _holdStart = 0;
  let _flashTimer = null;
  const _HOLD_MS = 1500;

  function _restoreIcon() {
    if (_flashTimer) { clearTimeout(_flashTimer); _flashTimer = null; }
    iconSpan.innerHTML = _DEL_TRASH_SVG;
    delBtn.style.color = _DEL_REST_COLOR;
    delBtn.style.borderColor = _DEL_REST_BORDER;
    delBtn.title = holdTitle || 'Hold 1.5s to delete';
  }

  function _clearHold() {
    if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
    _holdStart = 0;
    progress.style.width = '0%';
    delBtn.style.borderColor = _DEL_REST_BORDER;
    delBtn.style.color = _DEL_REST_COLOR;
    delBtn.title = holdTitle || 'Hold 1.5s to delete';
  }

  function _startHold() {
    // A hold takes priority over any lingering "saved" flash.
    if (_flashTimer) _restoreIcon();
    _holdStart = Date.now();
    delBtn.style.borderColor = 'rgba(var(--danger-rgb),0.7)';
    delBtn.style.color = 'var(--danger)';
    delBtn.title = 'Release to cancel';
    const step = () => {
      if (!_holdStart) return;
      const elapsed = Date.now() - _holdStart;
      const pct = Math.min(100, (elapsed / _HOLD_MS) * 100);
      progress.style.width = pct + '%';
      if (pct < 100) {
        _holdTimer = setTimeout(step, 50);
      } else {
        _clearHold();
        onConfirm(delBtn);
      }
    };
    _holdTimer = setTimeout(step, 50);
  }

  delBtn.addEventListener('mousedown', e => { if (e.button === 0) _startHold(); });
  const _cancel = () => { if (_holdStart) _clearHold(); };
  delBtn.addEventListener('mouseup', _cancel);
  delBtn.addEventListener('mouseleave', _cancel);
  delBtn.addEventListener('touchstart', _startHold, { passive: true });
  delBtn.addEventListener('touchend', _cancel, { passive: true });
  delBtn.addEventListener('touchcancel', _cancel, { passive: true });

  return delBtn;
}

/** Per-ability "more" (⋯) menu — the row's only extra control. The delete action
 *  moved INTO the menu (was a row-level hold-to-delete trash button), so the
 *  title line holds just the toggle + ⋯. The menu's delete runs the same
 *  `_confirmDeleteAbility` flow (type DELETE to confirm) as the old button. */
function _buildAbilityMoreButton(memberId, row, abilities) {
  return _buildMoreButton(() => {
    const meta = abilities[memberId] || {};
    const deletable = !meta.placeholder && meta.kind !== 'credential' && meta.kind !== 'oauth' && meta.kind !== 'channel';
    return [
      { icon: 'trash-2', label: 'Delete ability', danger: true, disabled: !deletable,
        action: () => _confirmDeleteAbility(memberId, row) },
    ];
  }, { title: 'More options — delete this ability' });
}

/** The All Agents switch opens the same compact floating availability menu as the
 * individual-agent Abilities page. Enabled/disabled persists through the app API;
 * discoverable/visible remains a signed-in-admin presentation preference until the
 * catalog provides an app-level visibility field. */
function _abilityExposureKey(id) { return 'webagent.adminAbilityExposure.' + id; }
function _getAbilityExposure(id, enabled) {
  if (!enabled) {
    localStorage.removeItem(_abilityExposureKey(id));
    return 'off';
  }
  const saved = localStorage.getItem(_abilityExposureKey(id));
  return saved === 'discoverable' || saved === 'visible' ? saved : 'visible';
}
function _paintAbilityExposure(id, mode) {
  const row = document.getElementById('ac-ability-row-' + id);
  const toggle = document.getElementById('ac-ability-toggle-' + id);
  if (row) row.dataset.exposure = mode;
  if (toggle) {
    toggle.dataset.exposure = mode;
    toggle.setAttribute('aria-label', mode === 'off' ? 'Ability off — choose availability' : mode === 'discoverable' ? 'Ability on and discoverable — choose availability' : 'Ability on and visible — choose availability');
  }
}
function _openAbilityExposureMenu(toggle, id, canEdit, lockedOn) {
  const rect = toggle.getBoundingClientRect();
  const choose = async mode => {
    const desired = mode !== 'off';
    const wasEnabled = !!toggle.checked;
    const ok = desired === wasEnabled ? true : await _runAbilityToggle(toggle, id, desired);
    if (!ok) return;
    if (desired) localStorage.setItem(_abilityExposureKey(id), mode);
    else localStorage.removeItem(_abilityExposureKey(id));
    _paintAbilityExposure(id, mode);
  };
  openFloatingMenu(() => {
    const mode = _getAbilityExposure(id, !!toggle.checked);
    return [
      { icon: 'eye-off', label: 'Off', checked: mode === 'off', disabled: !canEdit || lockedOn, action: () => choose('off') },
      { icon: 'download', label: 'Discoverable', checked: mode === 'discoverable', disabled: !canEdit, action: () => choose('discoverable') },
      { icon: 'eye', label: 'Visible', checked: mode === 'visible', disabled: !canEdit, action: () => choose('visible') },
    ];
  }, rect.bottom + 4, rect.right - 180);
}

/** Whole-group delete button — deletes the entire plugins/abilities/<group>/ folder. */
function _buildGroupDeleteButton(groupId, groupName) {
  return _buildHoldDeleteButton(
    (btn) => _confirmDeleteGroup(groupId, groupName, btn.closest('.ac-group')),
    'Hold 1.5s to delete this whole ability group',
  );
}

async function _confirmDeleteAbility(abilityId, row) {
  try {
    const res = await _apiFetch('/api/v1/abilities/' + abilityId + '/delete-info');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const info = await res.json();
    if (info.status !== 'ok') throw new Error(info.detail || 'Unknown error');
    const ab = info.ability;
    if (!info.deletable) {
      _showDeleteMessage(row, ab.display_name + ' is ' + ab.status + ' — it cannot be deleted through the UI. Delete the file manually from plugins/abilities/.', 'error');
      return;
    }

    const fileList = ab.files.map(f => '  \u2022 ' + f.path).join('\n');
    const agentMsg = ab.agent_count > 0
      ? '\n\n\u26a0 ' + ab.agent_count + ' agent(s) have this ability enabled and will have it disabled.'
      : '';

    const msg = 'Delete "' + ab.display_name + '"?\n\nThis will permanently delete:\n' + fileList + agentMsg + '\n\n' + info.warning + '\n\nThis cannot be undone. Type DELETE to confirm.';
    const confirmText = prompt(msg, '');
    if (confirmText !== 'DELETE') {
      _showDeleteMessage(row, 'Deletion cancelled — type DELETE to confirm.', 'error');
      return;
    }

    const delRes = await _apiFetch('/api/v1/abilities/' + abilityId, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ confirmed: true }),
    });
    if (!delRes.ok) throw new Error('HTTP ' + delRes.status);
    const data = await delRes.json();
    if (data.status === 'ok') {
      _showDeleteMessage(row, '\u2713 "' + (data.deleted_files?.[0]?.replace('.py','') || abilityId) + '" deleted. Files removed from ' + (data.disabled_on_agents || 0) + ' agent(s). Restart the server to fully clear.', 'success');
      row.style.transition = 'opacity 0.5s, height 0.5s, margin 0.5s';
      row.style.opacity = '0';
      row.style.height = '0';
      row.style.margin = '0';
      row.style.padding = '0';
      row.style.overflow = 'hidden';
      setTimeout(() => { if (row.parentNode) row.parentNode.removeChild(row); }, 600);
    } else {
      _showDeleteMessage(row, data.message || 'Deletion failed', 'error');
    }
  } catch (e) {
    _showDeleteMessage(row, 'Error: ' + e.message, 'error');
  }
}

// Delete-flow feedback (cancelled / error / success). The per-row status slot
// was removed when toggle saves moved onto the delete button, so this creates
// (or reuses) its own transient message line inside the row.
function _showDeleteMessage(row, msg, type) {
  if (!row) return;
  let statusEl = row.querySelector('.ac-del-message');
  if (!statusEl) {
    statusEl = document.createElement('span');
    statusEl.className = 'ac-del-message';
    statusEl.style.cssText = 'display:block;flex-basis:100%;font-size:11px;margin-top:4px;white-space:pre-wrap;';
    row.appendChild(statusEl);
  }
  statusEl.textContent = type === 'error' ? '\u26a0 ' + msg : '\u2713 ' + msg;
  statusEl.style.color = type === 'error' ? 'var(--danger)' : 'var(--success)';
  statusEl.style.display = 'block';
  // Show success messages permanently; errors auto-hide.
  if (type === 'error') {
    setTimeout(() => { if (statusEl.parentNode) statusEl.style.display = 'none'; }, 8000);
  }
}

// ── Admin-specific: delete a WHOLE ability group (the entire folder) ──────────

async function _confirmDeleteGroup(groupId, groupName, groupEl) {
  try {
    const res = await _apiFetch('/api/v1/abilities/group/' + encodeURIComponent(groupId) + '/delete-info');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const info = await res.json();
    if (info.status !== 'ok') throw new Error(info.detail || 'Unknown error');
    const g = info.group;
    if (!info.deletable) {
      alert(info.warning || ((g.name || groupName) + ' cannot be deleted.'));
      return;
    }

    const abilityList = (g.abilities || []).map(a => '  • ' + a.display_name).join('\n');
    const usageMsg = g.agent_usage_count > 0
      ? '\n\n⚠ ' + g.agent_usage_count + ' agent connection(s) will be removed.'
      : '';
    const msg = 'Delete the ENTIRE "' + (g.name || groupName) + '" group?\n\n'
      + 'This permanently deletes the folder plugins/abilities/' + g.folder + '/ '
      + 'and all ' + g.total_files + ' file(s) in it, removing these abilities:\n'
      + (abilityList || '  (no abilities)') + usageMsg + '\n\n' + (info.warning || '')
      + '\n\nThis cannot be undone. Type DELETE to confirm.';
    const confirmText = prompt(msg, '');
    if (confirmText !== 'DELETE') return;

    const delRes = await _apiFetch('/api/v1/abilities/group/' + encodeURIComponent(groupId), {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ confirmed: true }),
    });
    if (!delRes.ok) throw new Error('HTTP ' + delRes.status);
    const data = await delRes.json();
    if (data.status === 'ok' && groupEl) {
      groupEl.style.transition = 'opacity 0.5s, height 0.5s, margin 0.5s';
      groupEl.style.opacity = '0';
      groupEl.style.height = '0';
      groupEl.style.margin = '0';
      groupEl.style.overflow = 'hidden';
      setTimeout(() => { if (groupEl.parentNode) groupEl.parentNode.removeChild(groupEl); }, 600);
    } else if (data.status !== 'ok') {
      alert(data.message || 'Group deletion failed');
    }
  } catch (e) {
    alert('Error deleting group: ' + e.message);
  }
}

// ── Admin-specific: integration save ───────────────────────────────────────────
// These call the existing save functions in app-config.js (window globals).
// The caller wires these in the callbacks config.

// ── Config ROWS builder for non-simple abilities ────────────────────────────
// Non-simple abilities (meta.simple === false) need extra settings. This builder
// fetches the companion JSON schema from the API and returns the wired setting
// ROWS (no wrapping box) so the caller can drop them into the SAME unified
// `.ac-list` as the ability's tool rows — config knobs and tools then read as one
// continuous table. Returns { nodes, note } (or null when there are no settings).

async function _buildConfigRows(abilityId, meta, canEdit) {
  try {
    const res = await _apiFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/config-schema');
    if (!res.ok) return null;
    const schema = await res.json();
    if (!schema || !schema.settings || !schema.settings.length) return null;

    // Pre-fill with the APP-LEVEL (admin scope) values saved earlier, so the
    // fields reflect what's stored after a reload (not the schema defaults).
    const current = await _loadAbilityConfig(abilityId);

    // Build BARE setting rows via the shared builder (same stacked `.ac-ability-
    // row` as a tool row), so the two sister ability tables stay mirrored.
    // (SISTER-PANEL: AGENT-ABILITY-TABLE.)
    const tmp = document.createElement('div');
    tmp.innerHTML = _buildSettingsRowsHtml(schema, current, 'ac-config-field', 'data-config-key');

    // Wire each setting to save-on-change with the shared overlay indicator
    // (spinner → ✓ over the control, or ⚠ + revert on failure). This mirrors the
    // agent panel's per-agent settings save. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
    const fields = [...tmp.querySelectorAll('.ac-config-field')];
    if (!canEdit) fields.forEach(field => { field.disabled = true; });
    const collect = () => {
      const settings = {};
      fields.forEach(f => { settings[f.dataset.configKey] = f.value; });
      return settings;
    };
    if (canEdit) fields.forEach(f => {
      const ctrl = f.closest('.ac-config-control');
      let prevVal = f.value;   // last-saved value, for revert on failure
      f.addEventListener('change', async () => {
        f.disabled = true;
        _markSaving(ctrl);                       // spinner over the field
        try {
          await _saveAbilityConfig(abilityId, collect());
          prevVal = f.value;
          _flashSaveCheck(ctrl, true);           // ✓
        } catch (e) {
          f.value = prevVal;                     // revert to the last saved value
          _flashSaveCheck(ctrl, false);          // ⚠
        } finally {
          f.disabled = false;
        }
      });
    });
    return { nodes: [...tmp.children], note: schema.note || '' };
  } catch (e) {
    return null;
  }
}

// Auth-aware, base-path-aware fetch for every app-level table request. The All
// Agents surface can run under an app-shell alias or subdirectory deployment, so
// raw root-relative URLs otherwise save to the wrong origin path.
function _apiFetch(path, opts = {}) {
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
  return fetch(apiPath(path), opts);
}

// Load the saved app-level (admin scope) config values for an ability, so the
// fields pre-fill with what was stored. Returns a flat {key: value} map (empty
// on any failure — the fields then just show their schema defaults). Backed by
// `GET /api/v1/abilities/{id}/config` → app/admin/ability_config.py.
async function _loadAbilityConfig(abilityId) {
  try {
    const res = await _apiFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/config');
    if (!res.ok) return {};
    const data = await res.json();
    return (data && data.ability_settings && typeof data.ability_settings === 'object')
      ? data.ability_settings : {};
  } catch (_) {
    return {};
  }
}

// Persist app-level ability config settings (admin scope). Backed by
// app/admin/ability_config.py `set_ability_config` via
// `PUT /api/v1/abilities/{id}/config`. Throws on a non-OK response so the caller
// shows ⚠ + revert.
async function _saveAbilityConfig(abilityId, settings) {
  const res = await _apiFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ability_settings: settings }),
  });
  if (!res.ok) throw new Error('save failed');
}

// ── Generic credentials form for credential-bearing abilities ────────────────
// The form itself now lives in shared/dom-utils.js as `buildCredentialsSection`
// so the admin table AND the per-agent Abilities tab render the IDENTICAL secret
// entry (the SISTER-PANEL contract, one implementation). The admin table passes
// an `onSaved` that drops its catalog cache so a saved secret immediately flips
// the row's configured-state. The common-credential framework backing it is
// app/abilities/credentials.py.

// ── Admin-specific: GLOBAL per-tool defaults adapter ─────────────────────────
// ISOLATED on purpose: this is the ONLY place admin tool-default endpoints are
// referenced. The per-tool PERMISSION (Auto/Ask/Deny) + VISIBILITY (Sent/
// Discover) controls write the app-wide default for a tool (the seed every
// agent inherits until overridden). Endpoints:
//   GET  /admin/integrations/tool-defaults              -> { tools: { name: {permission, visibility} } }
//   PUT  /admin/integrations/tool-defaults/{toolName}   <- { permission?, visibility? } (backend merges)
// Send only the changed dimension; the backend merges with the stored value.

// Tools that inherently pause for confirmation (built-in destructive nature) —
// populated from the tool-defaults endpoint's `confirm_tools`. Used so a
// write/destructive tool shows at its true "Ask" floor with Auto locked, instead
// of misleadingly defaulting to "Auto". (SISTER-PANEL: AGENT-ABILITY-TABLE — the
// per-agent table gets the same floor via each tool's `ceiling` from the API.)
let _ADMIN_CONFIRM_TOOLS = new Set();

async function _loadGlobalToolDefaults() {
  try {
    const res = await _apiFetch('/admin/integrations/tool-defaults');
    if (!res.ok) return {};
    const data = await res.json();
    _ADMIN_CONFIRM_TOOLS = new Set((data && data.confirm_tools) || []);
    return (data && data.tools) || {};
  } catch (e) {
    return {};
  }
}

async function _saveGlobalToolDefault(toolName, patch) {
  const res = await _apiFetch('/admin/integrations/tool-defaults/' + encodeURIComponent(toolName), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error('save failed');
}

// Build the per-ability tool ROWS for the admin panel. Each tool's controls write
// the GLOBAL default via the adapter above. `defaults` is the map from
// _loadGlobalToolDefaults(); rows seed from it (default auto / always). Returns an
// ARRAY of `.ac-ability-row` elements so the caller can drop them into the SAME
// unified `.ac-list` as the ability's config rows.
//
// `toolNames` is THE source of truth — the ability's tool list straight from the
// fetched catalog (`abilities[id].tools`), passed in by the caller. It is the
// complete, drop-in-discovered set (all 57 abilities, every tool), and is the
// SAME data the per-agent table resolves, so the two sister panels list the same
// tools. We fall back to the stale `ABILITY_TO_TOOLS` map only if the catalog
// somehow handed us no tools (offline fallback). Previously this read ONLY the
// stale map — which omits most abilities — so the admin saw tools for just the
// handful hard-coded there and none for the rest.
// ── Admin-specific: the "Skill" row (view / edit the ability's .skill.md) ─────
// Sits at the TOP of an ability's expanded body, above its config settings. The
// Edit button reveals an inline editor that lazy-loads the ability's shared
// <id>.skill.md (GET /api/v1/abilities/{id}/skill) and saves it back
// (PUT same path). Editing writes a repo-tracked file shared by every agent that
// uses the ability — inline-skill abilities come back editable:false (read-only).
function _buildSkillRow(abilityId, meta, canEdit) {
  const wrap = document.createElement('div');
  wrap.className = 'ac-skill-row';
  wrap.style.cssText = 'margin:8px 0;';

  // Header line: label + summary + Edit toggle.
  const head = document.createElement('div');
  head.className = 'limits-row';
  head.style.cssText = 'display:flex;align-items:center;gap:8px;';
  const summary = (meta && meta.skill_summary) ? meta.skill_summary : 'How-to document for this ability.';
  head.innerHTML =
    `<span class="limits-name" style="display:flex;align-items:center;gap:6px;">`
    + `<i data-lucide="book-open" style="width:14px;height:14px;"></i>Skill</span>`
    + `<span class="limits-value" style="flex:1;color:var(--fg-3);font-size:11px;">${_esc(summary)}</span>`;

  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.className = 'agents-btn';
  editBtn.textContent = canEdit ? 'Edit' : 'View';
  editBtn.style.cssText = 'font-size:11px;padding:3px 10px;flex-shrink:0;';
  head.appendChild(editBtn);
  wrap.appendChild(head);

  // Editor panel — hidden until Edit is clicked; content lazy-loaded once.
  const panel = document.createElement('div');
  panel.style.cssText = 'display:none;margin-top:6px;';
  const ta = document.createElement('textarea');
  ta.className = 'agents-input';
  ta.style.cssText = 'width:100%;min-height:200px;font-family:monospace;font-size:12px;resize:vertical;white-space:pre;';
  ta.spellcheck = false;
  const bar = document.createElement('div');
  bar.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:6px;';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'agents-btn';
  saveBtn.textContent = 'Save';
  saveBtn.style.cssText = 'font-size:11px;padding:4px 12px;';
  const status = document.createElement('span');
  status.style.cssText = 'font-size:11px;';
  bar.appendChild(saveBtn);
  bar.appendChild(status);
  panel.appendChild(ta);
  panel.appendChild(bar);
  wrap.appendChild(panel);

  // Keep clicks inside the editor from bubbling to row/group expand handlers.
  panel.addEventListener('click', (e) => e.stopPropagation());

  let _loaded = false;
  const _flash = (ok, msg) => {
    status.textContent = msg || (ok ? 'Saved' : 'Error');
    status.style.color = ok ? 'var(--success)' : 'var(--danger)';
    if (ok) setTimeout(() => { status.textContent = ''; }, 2500);
  };

  async function _loadOnce() {
    if (_loaded) return;
    _loaded = true;
    ta.value = 'Loading…';
    ta.disabled = true;
    try {
      const res = await _apiFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/skill');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      ta.value = data.content || '';
      if (!canEdit || data.editable === false) {
        ta.disabled = true;
        saveBtn.disabled = true;
        _flash(false, !canEdit
          ? 'Only an app admin can edit this skill.'
          : data.inline
          ? 'Defined inline in the descriptor — read-only here.'
          : 'This skill is read-only.');
      } else {
        ta.disabled = false;
        saveBtn.disabled = false;
        if (!data.exists) _flash(true, 'No file yet — saving creates ' + (data.path || abilityId + '.skill.md'));
      }
    } catch (e) {
      _loaded = false;  // allow a retry on next open
      ta.value = '';
      ta.disabled = false;
      _flash(false, 'Could not load: ' + e.message);
    }
  }

  editBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const open = panel.style.display === 'none';
    panel.style.display = open ? 'block' : 'none';
    editBtn.textContent = open ? 'Close' : 'Edit';
    if (open) await _loadOnce();
  });

  saveBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (saveBtn.disabled) return;
    saveBtn.disabled = true;
    _flash(true, 'Saving…');
    try {
      const res = await _apiFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/skill', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ content: ta.value }),
      });
      if (!res.ok) {
        let detail = 'HTTP ' + res.status;
        try { const d = await res.json(); if (d && d.detail) detail = d.detail; } catch (_) {}
        throw new Error(detail);
      }
      _flash(true, 'Saved');
    } catch (err) {
      _flash(false, err.message);
    } finally {
      saveBtn.disabled = false;
    }
  });

  return wrap;
}

// ╔══════════════════════════════════════════════════════════════════════════════╗
// ║  MAIN BUILD FUNCTION                                                       ║
// ╚══════════════════════════════════════════════════════════════════════════════╝

/**
 * Build the admin ability table into `container`.
 *
 * @param {HTMLElement} container - DOM element to render into (e.g. #ac-abilities-compact)
 * @param {object} cfg
 * @param {object} cfg.abilityStates - { [abilityId]: boolean } current enable state
 * @param {object} cfg.integrationStates - { [providerId]: { configured: bool, kind: 'channel'|'oauth'|'generic' } }
 * @param {object} [cfg.callbacks] - { onEnableChannel, onDisableChannel, onUnconfigureProvider }
 * @param {boolean} [cfg.showSearch] - show search bar (default true)
 * @param {boolean} [cfg.canEdit] - app-level controls are editable (default true)
 */
export async function build(container, cfg) {
  if (!container) return null;

  const state = { container, grid: null, searchGroups: [], triSyncs: [] };

  // Show a spinner while the catalog + global tool defaults load. It's cleared
  // by `container.innerHTML = ''` below once everything is ready, after which
  // the groups are revealed one-by-one.
  // SISTER-PANEL note: the agent table (`agent-ability-table.js`) loads in staged
  // steps (group heads → per-agent toggle statuses → group contents on expand)
  // because its enable state lives in a SEPARATE per-agent /connections fetch. The
  // admin table needs no such staging: every ability's app-level enable state is
  // baked into this single catalog fetch (`abilities[id].enabled`), so its group
  // tris render correctly on the first paint with nothing extra to wait for. The
  // visual component (groups, rows, tris, tools) is identical either way.
  container.innerHTML = '';
  container.appendChild(_buildAbilityTreeLoader());

  const cat = await loadAbilityCatalog();
  const groups = cat.groups;
  const abilities = cat.abilities || {};
  // credential row detection now uses abilities[id].kind === "credential" or "oauth"

  const abilityStates = cfg.abilityStates || {};
  const integrationStates = cfg.integrationStates || {};
  const cb = cfg.callbacks || {};
  const showSearch = cfg.showSearch !== false;
  const canEdit = cfg.canEdit !== false;

  // Populate runtime state from the caller's snapshot.
  for (const [id, st] of Object.entries(abilityStates)) {
    _abilityStates[id] = !!st;
  }

  // Global per-tool defaults (seeds each tool's Permission/Visibility controls).
  const globalToolDefaults = await _loadGlobalToolDefaults();

  container.innerHTML = '';

  // ── Search / chat pill ───────────────────────────────────────────────────────
  // Inserted at the TOP after the grid is built (so onFilter can reference the
  // populated grid + searchGroups). See end of build().

  const grid = document.createElement('div');
  grid.className = 'ac-list';
  container.appendChild(grid);

  // { groupEl, rows: [{ el, haystack }] }

  // ── Build host ability groups (Administrator, Core, Productivity, Web) ──────
  for (const group of groups) {
    const groupMeta = abilities[group.id] || {};
    const groupEl = _buildGroup(group.id, group.name, group.icon, group.color, group.desc);
    const head = groupEl.querySelector('.ac-group-head');
    const bodyEl = groupEl.querySelector('.ac-group-body');

    const togglable = [];
    const memberRows = [];

    for (const memberId of (group.members || [])) {
      const meta = abilities[memberId] || {};
      // Dual-surface safety services (currently Context Control) belong in the
      // per-agent Abilities tab and App Settings ▸ App Functions.  Do not render
      // a third duplicate row in the admin Agent Tools table.
      if (meta.app_function) continue;
      const kind = meta.kind || "ability";
      const isPlaceholder = meta.placeholder || kind === "placeholder";
      const displayName = meta.display_name || memberId;

      if (isPlaceholder) {
        // Placeholder row
        const row = _buildMemberRow(memberId, _iconHtml(meta.icon || "plug", "18px"), meta.color || "var(--brand)", displayName, meta.note || meta.description || "");
        row.appendChild(_buildComingSoonPill());
        bodyEl.appendChild(row);
        memberRows.push({
          el: row,
          haystack: (displayName + " " + memberId + " " + (meta.note || meta.description || "") + " " + group.name).toLowerCase(),
        });
        continue;
      }

      if (kind === ("credential") || kind === "oauth" || kind === "channel") {
        // Credential members get a slot — the caller fills them later
        // (mirrors _placeWebCredentialRows in app-config.js).
        const slot = document.createElement('div');
        slot.id = 'ac-group-slot-' + memberId;
        bodyEl.appendChild(slot);
        // Credential members never participate in the tri-toggle.
        continue;
      }

      const row = _buildMemberRow(memberId, _iconHtml(meta.icon || 'plug', '18px'), meta.color || 'var(--brand)', displayName, meta.description || '');
      // Seed the toggle from the catalog's resolved on/off (stored admin choice ▸
      // descriptor default), baked into the catalog server-side. This is the
      // authoritative state, so the switch renders correctly on first paint with
      // no dependency on the host's slower /admin/integrations state fetch.
      if (typeof meta.enabled === 'boolean') _abilityStates[memberId] = meta.enabled;
      // A locked-on safety ability (e.g. Context Control) is always enabled and
      // its toggle is fixed ON — it cannot be turned off.
      const lockedOn = !!meta.locked_on;
      if (lockedOn) _abilityStates[memberId] = true;
      const _lockTitle = 'Always on — safety device, cannot be deactivated';
      const { toggle, toggleWrap } = _appendToggle(
        row, 'ac-ability-toggle-' + memberId,
        lockedOn ? true : !!_abilityStates[memberId], canEdit && !lockedOn,
        lockedOn ? _lockTitle : undefined,
        lockedOn ? _lockTitle : undefined);
      if (lockedOn && toggleWrap) toggleWrap.classList.add('ac-ability-toggle-locked');

      // EVERY member row is expandable. Expand chevron sits LEFT of the icon
      // (after the row indent); the ⋯ more button is the LAST child so it lines
      // up with the group row's delete button on the same right edge.
      const chevron = document.createElement('span');
      chevron.className = 'ac-row-chevron ac-row-chevron--left';
      chevron.innerHTML = _chevronSvg();
      row.insertBefore(chevron, row.firstChild);

      bodyEl.appendChild(row);

      // The body holds (a) the config settings (non-simple abilities) or a
      // "No additional settings" note, and (b) a per-ability tools sub-list
      // whose Permission/Visibility controls write GLOBAL tool defaults.

      const configBody = document.createElement('div');
      configBody.className = 'ac-ability-body';
      configBody.id = 'ac-config-body-' + memberId;

      // (0) Skill row — the ability's how-to document, above the configuration.
      // Edit opens an inline viewer/editor for the shared <id>.skill.md file.
      configBody.appendChild(_buildSkillRow(memberId, meta, canEdit));

      // (0.5) Credentials — generic form for abilities that declare a
      // `credentials` block (drop-in; no bespoke card). See buildCredentialsSection (dom-utils).
      if (meta.has_credentials) {
        configBody.appendChild(buildCredentialsSection(memberId, {
          canEdit,
          onSaved: () => { invalidateAbilityCatalog(); },
        }));
      }

      // (a)+(b) Unified settings + tools list. The ability's config knobs (async,
      // non-simple abilities only) AND its tool rows render as ONE continuous run
      // of rows that sits DIRECTLY in the expanded body — no bordered sub-table
      // box around it (the frame is stripped in CSS; see `.ac-config-settings`).
      // Every row is the SAME stacked `.ac-ability-row` (title + control inline,
      // description below). (SISTER-PANEL: AGENT-ABILITY-TABLE.)
      const unifiedList = _buildAbilitySettingsList();
      // Tool rows append AFTER this anchor; config rows (async) insert BEFORE it —
      // so config always lands above the tools regardless of which finishes first.
      const toolsAnchor = document.createComment('tools');
      unifiedList.appendChild(toolsAnchor);
      // Drop the whole list if it ends up empty (e.g. a simple, tool-less
      // ability) so the body stays clean and CSS keeps it hidden.
      const pruneUnified = () => {
        if (!unifiedList.querySelector('.ac-ability-row')) unifiedList.remove();
      };

      // (a) Config rows. Only non-simple abilities have settings; when there are
      // none nothing is rendered (an empty list is pruned).
      if (meta.simple === false) {
        const ph = document.createElement('div');
        ph.className = 'conn-loading';
        ph.style.cssText = 'padding:8px;font-size:11px;';
        ph.textContent = 'Loading settings…';
        unifiedList.insertBefore(ph, toolsAnchor);
        _buildConfigRows(memberId, meta, canEdit).then(loaded => {
          // MOVE the built nodes in (they carry change-to-save listeners wired in
          // _buildConfigRows — re-serializing via innerHTML would drop them).
          ph.remove();
          if (loaded && loaded.nodes && loaded.nodes.length) {
            loaded.nodes.forEach(n => unifiedList.insertBefore(n, toolsAnchor));
            if (loaded.note) {
              const noteEl = document.createElement('div');
              noteEl.className = 'ac-config-note';
              noteEl.textContent = loaded.note;
              configBody.appendChild(noteEl);
            }
          }
          pruneUnified();
        });
      }

      // (b) Tool rows (global defaults) — shown in full when the row opens. Tool
      // names come from the catalog (`meta.tools`, the drop-in-discovered set), so
      // EVERY ability lists its real tools — not just the handful in the stale
      // ABILITY_TO_TOOLS fallback (used only if the catalog handed us no tools).
      const _adminToolNames = (Array.isArray(meta.tools) && meta.tools.length)
        ? meta.tools
        : ((ABILITY_TO_TOOLS || {})[memberId] || []);
      for (const name of _adminToolNames) {
        const d = globalToolDefaults[name] || {};
        // A tool that inherently confirms (write/destructive) floors at "Ask": it
        // displays as Ask with Auto locked, and can only be tightened to Deny. Reads
        // keep the plain Auto default. The admin's stored override (if any) still wins
        // when it is at least as strict as the floor.
        const isConfirm = _ADMIN_CONFIRM_TOOLS.has(name);
        const ceiling = isConfirm ? 'ask' : 'auto';
        const permMode = d.permission || (isConfirm ? 'ask' : 'auto');
        const mode = d.visibility === 'discoverable' ? 'discoverable' : 'always';
        unifiedList.appendChild(_buildToolRow(
          {
            name,
            description: (meta.tool_descriptions && meta.tool_descriptions[name]) || '',
            mode,
            locked: false,
            destructive: isConfirm,
            permMode,
            ceiling,
          },
          {
            abilityId: memberId,
            canEdit,
            // The App catalog is the sole place that marks an absent per-tool
            // Markdown document; per-agent callers intentionally do not opt in.
            showMissingSkill: true,
            hasToolSkill: Object.prototype.hasOwnProperty.call(meta.skill_tool_sections || {}, name),
            // Only the App → Agent tools & abilities admin surface may save skills
            // or globally override a tool's display description.
            toolSkillEditable: canEdit,
            toolDescriptionEditable: canEdit,
            showPerm: true,
            // Admin = ceiling only (enable + permission). Visibility/discovery is
            // tuned PER AGENT in the Abilities tab, so the eye toggle is omitted here.
            showVisibility: false,
            onPermissionChange: (toolName, m) =>
              // Return the save's outcome so the row shows spinner → Saved / Failed.
              _saveGlobalToolDefault(toolName, { permission: m }).then(() => true).catch(() => false),
          },
        ));
      }

      configBody.appendChild(unifiedList);
      // Prune now if there are no config rows to wait for (config prunes itself
      // once its async load resolves).
      if (meta.simple !== false) pruneUnified();

      bodyEl.appendChild(configBody);
      // Paint after insertion so the row and toggle receive their exposure data attributes.
      _paintAbilityExposure(memberId, _getAbilityExposure(memberId, !!_abilityStates[memberId]));

      // Click row to expand / collapse. `.ac-row-open` on the row reveals the
      // (otherwise clamped) description so the title row grows to fit it;
      // `.ac-body-open` reveals the settings/tools body (an empty body stays
      // hidden via CSS). Ignore clicks on the toggle, delete button, and the
      // group tri-toggle (the settings/tools rows live in the body, a sibling of
      // this header row, so their clicks never reach here).
      row.addEventListener('click', (e) => {
        if (e.target.closest('.ac-ability-toggle-wrap, button, .ac-tri')) return;
        const open = row.classList.toggle('ac-row-open');
        configBody.classList.toggle('ac-body-open', open);
        chevron.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
      });
      row.style.cursor = 'pointer';

      togglable.push({
        id: memberId,
        isEnabled: () => !!_abilityStates[memberId],
      });

      memberRows.push({
        el: row,
        haystack: (displayName + ' ' + memberId + ' ' + (meta.description || '') + ' ' + group.name).toLowerCase(),
      });
    }

    // ── Group status card (always present, consistent footprint) ──
    //   • >=1 togglable ability member -> live status (Disabled · Partial ·
    //     Enabled), re-synced whenever a member toggles.
    //   • 0 togglable members (placeholder-only / credential / integration
    //     groups) -> neutral "—" card; the host page (agent-settings.js) paints
    //     its own configured-count onto those. Bulk toggling is gone.
    if (togglable.length) {
      const status = _appendGroupStatus(head, 'ac-group-status-' + group.id, 'off', 'Disabled \u00b7 Partial \u00b7 Enabled — read-only summary of this group');
      const paint = () => {
        const onCount = togglable.filter(m => m.isEnabled()).length;
        status.dataset.state = onCount === 0 ? 'off' : (onCount === togglable.length ? 'on' : 'mixed');
        _paintGroupStatus(status);
      };
      paint();
      // Member toggle changes already re-paint via `_resyncParentStatus`
      // (_runAbilityToggle); `state.syncAllTriToggles` covers external loads.
    } else {
      _appendGroupStatus(head, 'ac-group-status-' + group.id, 'na', 'No on/off abilities in this group');
    }

    // ── Group delete button (admin) — deletes the whole folder. Last child of
    //    the head so it aligns with the per-ability delete buttons below. ──
    if (canEdit) head.appendChild(_buildGroupDeleteButton(group.id, group.name));

    grid.appendChild(groupEl);
    state.searchGroups.push({ groupEl, rows: memberRows });
  }

  // Match the individual-agent Abilities page: the switch is a compact menu
  // trigger, and the selected availability option owns the persisted state change.
  grid.querySelectorAll('[data-ability]').forEach(rowEl => {
    const id = rowEl.dataset.ability;
    const toggleEl = document.getElementById('ac-ability-toggle-' + id);
    if (!toggleEl) return;
    const lockedOn = !!(abilities[id] && abilities[id].locked_on);
    toggleEl.setAttribute('aria-haspopup', 'menu');
    toggleEl.addEventListener('click', (event) => {
      event.preventDefault();
      if (lockedOn) {
        _flashStatusText(_abilityStatusEl(id), 'Always on', 'warn');
        return;
      }
      _openAbilityExposureMenu(toggleEl, id, canEdit, lockedOn);
    });
  });

  // ── Search-only field ────────────────────────────────────────────────────────
  // TYPE → live filter. Manager chat belongs to the shared Agents-page launcher.
  // SISTER-PANEL: AGENT-ABILITY-TABLE — mirrored in agent-ability-table.js, with
  // ONE intentional divergence in MOUNT LOCATION (not look/behaviour): the agent
  // card builds this pill INLINE above the table (showSearch defaults on). The
  // admin Agent Settings PAGE passes `showSearch:false` and instead mounts the same
  // shared pill at the BOTTOM of the page (App-Settings page-assistant style),
  // driving the filter through the exposed `state.runFilter` below. Same component,
  // different home — like the documented progressive-load divergence, not a
  // sister-panel violation.
  if (showSearch) {
    const searchPill = buildAbilitySearchPill(container, {
      onFilter: (q) => _runAdminFilter(q, grid, state.searchGroups),
      chatEnabled: false,
    });
    container.insertBefore(searchPill.wrap, container.firstChild);
    state.searchPill = searchPill;
  }

  // Render Lucide icons
  if (typeof lucide !== 'undefined' && lucide.createIcons) {
    lucide.createIcons();
  }

  // Reveal the groups one-by-one (each unfurls vertically) now that they're
  // all in the DOM.
  _staggerRevealGroups(grid);

  state.grid = grid;
  // state.searchGroups already populated

  // Let an externally-mounted search pill (the admin Agent Settings page mounts it
  // at the bottom — see the showSearch note above) drive the live filter without
  // owning the grid.
  state.runFilter = (q) => _runAdminFilter(q, grid, state.searchGroups);
  // A route-level search surface (such as the Agents Ask or Search launcher) can
  // own the visible input while this table remains free of a duplicate composer.
  if (typeof cfg.onSearchController === 'function') cfg.onSearchController(state.runFilter);

  // Return a handle so the caller can re-sync group status cards after data loads.
  // (Method name kept for the host page's existing call — it now repaints the
  // read-only status pills rather than tri-toggles.)
  state.syncAllTriToggles = () => {
    grid.querySelectorAll('.ac-group-status').forEach(status => {
      // Credential/integration groups have no [data-ability] rows (their members
      // are slots the host fills) — the host owns their cards and paints them
      // itself, so skip those (an empty row-set would force them to "off").
      const groupEl = status.closest('.ac-group');
      if (!groupEl) return;
      const body = groupEl.querySelector('.ac-group-body');
      const rows = body ? [...body.querySelectorAll('[data-ability]')] : [];
      if (!rows.length) return;
      const onCount = rows.filter(r => !!_abilityStates[r.dataset.ability]).length;
      status.dataset.state = onCount === 0 ? 'off' : (onCount === rows.length ? 'on' : 'mixed');
      _paintGroupStatus(status);
    });
  };

  return state;
}


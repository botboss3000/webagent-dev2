'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — Prompts tab.
 *
 * ONE unified carousel of prompt tiles: the prompt SLOTS (system / agent / user /
 * skills / tasks / misc / automation / bootstrap-tools …) followed by the agent's
 * SKILLS, all in a single horizontal strip — mirroring the base Agents-page squares
 * carousel. Clicking any tile opens its editor in ONE shared detail area BELOW the
 * strip (only one tile open at a time). Slots and skills stay functionally distinct
 * (different tile look, editor, and save endpoint) but read as one group.
 *
 * Layout note (fixes the old chevron overlap): the open editor is a SIBLING of the
 * squares wrap, NOT a child of it. The scroll chevrons are absolutely positioned to
 * span their wrap's height, so keeping the editor outside the wrap means the chevrons
 * only cover the tile strip — never the textarea below. Same structure as the base
 * Agents page (#agents-detail-region sits outside .agents-squares-wrap).
 */

import { app } from '../../../shared/js/state.js';
import { icon } from '../../../shared/js/icons.js';
import { _isMockAgent } from './state.js';
import { _esc, _makeAutosaveCheck, _flashSaved, _debounced } from './utils.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';
import { buildLevelSelector, copyPolicy, isBaseLevel, loadMemberLevels, saveMemberLevel } from './member-level-policy.js';

export async function _renderPromptsTab(body, agent, panelEl) {
  if (_isMockAgent(agent)) {
    body.innerHTML = '<div style="padding:20px;color:var(--fg-3);font-size:13px;text-align:center;">Save this agent first to configure prompts.</div>';
    return;
  }

  body.innerHTML = '';
  let levels = [];
  try { levels = await loadMemberLevels(agent); } catch (error) {
    const notice = document.createElement('div'); notice.className = 'member-policy-error';
    notice.textContent = `Member levels could not be loaded: ${error.message}`;
    body.appendChild(notice);
  }
  panelEl._memberPromptLevel ||= '__base_agent__';
  body.appendChild(buildLevelSelector({
    levels,
    selected: panelEl._memberPromptLevel,
    onChange: (slug) => { panelEl._memberPromptLevel = slug; _renderPromptsTab(body, agent, panelEl); },
  }));
  if (!isBaseLevel(panelEl._memberPromptLevel)) {
    const profile = levels.find(level => level.slug === panelEl._memberPromptLevel);
    if (profile) { await _renderProfilePromptMode(body, agent, profile); return; }
    panelEl._memberPromptLevel = '__base_agent__';
  }

  const host = document.createElement('div'); host.className = 'agents-field-group'; host.dataset.role = 'prompts-host';
  const list = document.createElement('div'); list.className = 'agents-slots-list'; list.dataset.role = 'prompts-list'; list.style.marginTop = '4px';
  // Spinner until BOTH slots + skills have loaded, so the merged strip appears in one piece.
  list.innerHTML = '<div class="agents-spinner" style="margin:12px auto;display:block;"></div>';
  host.appendChild(list);
  body.appendChild(host);

  // Slots are editable when the agent is a custom (user-owned) agent. Skills need
  // admin on top of that — resolved from the /skills payload once it loads.
  panelEl._slotsEditable = agent.source === 'custom';
  panelEl._skillsEditable = false;

  panelEl._slotsCheck = _makeAutosaveCheck();
  panelEl._skillsCheck = _makeAutosaveCheck();
  panelEl._slotsOnChange = _debounced(() => _saveSlotsNow(agent, panelEl, panelEl._slotsCheck), 800);
  panelEl._skillsOnChange = _debounced(() => _saveSkillsNow(agent, panelEl, panelEl._skillsCheck), 800);

  panelEl._slotState = { slots: [], overrides: {}, resetOverridesFor: new Set(), userRole: 'member', loaded: false };
  panelEl._skillsState = [];
  panelEl._skillsLoaded = false;
  panelEl._slotsLoadError = '';

  // Unified active pointer for the merged carousel: { kind:'slot'|'skill', idx } | null.
  panelEl._promptActive = null;

  _loadSlots(panelEl, agent);
  _loadSkills(panelEl, agent);
}

// ── Membership-level prompt overlay ───────────────────────────────────────────

async function _renderProfilePromptMode(body, agent, profile) {
  const policy = copyPolicy(profile);
  const wrap = document.createElement('section'); wrap.className = 'member-policy-editor';
  wrap.innerHTML = `<div class="member-policy-editor-title">${_esc(profile.name || profile.slug)} prompt overlay</div>
    <p class="member-policy-editor-hint">These instructions are appended to the inherited base prompt for this member level. Prompt slots and skills remain configured at the base-agent level.</p>`;

  const preview = document.createElement('details'); preview.className = 'member-policy-preview';
  const summary = document.createElement('summary'); summary.textContent = 'View inherited base prompt context'; preview.appendChild(summary);
  const pre = document.createElement('pre'); pre.textContent = 'Loading inherited prompt slots…'; preview.appendChild(pre);
  wrap.appendChild(preview);
  try {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(agent.id)}/my-prompts?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    const data = res.ok ? await res.json() : { slots: [] };
    pre.textContent = (data.slots || []).map(slot => `# ${slot.slot_name || slot.id || 'prompt'}\n${slot.override_content ?? slot.content ?? ''}`).join('\n\n') || 'No inherited prompt slots are available.';
  } catch (_) { pre.textContent = 'Inherited prompt context could not be loaded.'; }

  const label = document.createElement('label'); label.className = 'agents-field-label'; label.textContent = 'Level instructions';
  const textarea = document.createElement('textarea'); textarea.className = 'agents-textarea member-policy-overlay'; textarea.rows = 11;
  textarea.placeholder = 'Add level-specific instructions. Leave empty to use only the inherited base prompt.';
  textarea.value = String(policy.prompt_overlay || '');
  label.appendChild(textarea); wrap.appendChild(label);

  const actions = document.createElement('div'); actions.className = 'member-policy-actions';
  const status = document.createElement('span'); status.className = 'member-policy-save-status';
  const save = document.createElement('button'); save.type = 'button'; save.className = 'members-btn authorize'; save.textContent = 'Save level instructions';
  const reset = document.createElement('button'); reset.type = 'button'; reset.className = 'members-btn restrict'; reset.textContent = 'Reset to inherited';
  const persist = async (value) => {
    save.disabled = reset.disabled = true; status.textContent = 'Saving…';
    try {
      policy.prompt_overlay = value;
      const saved = await saveMemberLevel(agent, profile, policy);
      profile.policy = saved.policy || policy; textarea.value = String(profile.policy.prompt_overlay || '');
      status.textContent = 'Saved';
    } catch (error) { status.textContent = `Save failed: ${error.message}`; }
    finally { save.disabled = reset.disabled = false; }
  };
  save.addEventListener('click', () => persist(textarea.value));
  reset.addEventListener('click', () => persist(''));
  actions.append(save, reset, status); wrap.appendChild(actions); body.appendChild(wrap);
}

// ── Data loading ──────────────────────────────────────────────────────────────

async function _loadSlots(panelEl, agent) {
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/my-prompts?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      panelEl._slotState = { slots: data.slots || [], overrides: data.overrides || {}, resetOverridesFor: new Set(), userRole: data.user_role || 'member', loaded: true };
    } else {
      panelEl._slotState.loaded = true;
      panelEl._slotsLoadError = 'Failed to load prompt slots.';
    }
  } catch (e) {
    panelEl._slotState.loaded = true;
    panelEl._slotsLoadError = `Error: ${e.message}`;
  }
  _renderPromptCarousel(panelEl, agent);
}

async function _loadSkills(panelEl, agent) {
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/skills?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() });
    const data = res.ok ? await res.json() : { skills: [], user_role: 'member' };
    panelEl._skillsEditable = data.user_role === 'admin' && agent.source === 'custom';
    const seed = Array.isArray(data.skills) ? data.skills : [];
    panelEl._skillsState = seed.map(s => ({
      name: s.name || '', description: s.description || '', body: s.body || '',
      mode: s.mode === 'always_on' ? 'always_on' : 'selectable', enabled: s.enabled !== false,
    }));
  } catch (e) {
    panelEl._skillsState = [];
  }
  panelEl._skillsLoaded = true;
  _renderPromptCarousel(panelEl, agent);
}

// ── Icon / colour helpers for slot tiles ──────────────────────────────────────
const _SLOT_ICON_MAP = { system: 'terminal', agent: 'bot', user: 'user', skills: 'book', tasks: 'list-todo', misc: 'file-text', automation: 'clock', bootstrap_tools: 'wrench' };
const _SLOT_COLOR_MAP = { system: 'color-gray', agent: 'color-blue', user: 'color-teal', skills: 'color-purple', tasks: 'color-red', misc: 'color-gray', automation: 'color-teal', bootstrap_tools: 'color-red' };
function _slotIcon(name) { return _SLOT_ICON_MAP[name] || 'file-text'; }
function _slotColor(name) { return _SLOT_COLOR_MAP[name] || 'color-blue'; }

// ── Unified carousel render ───────────────────────────────────────────────────

function _renderPromptCarousel(panelEl, agent) {
  const list = panelEl.querySelector('[data-role="prompts-list"]');
  if (!list) return;
  // Hold the spinner until both halves are in — render the strip as one piece.
  if (!(panelEl._slotState.loaded && panelEl._skillsLoaded)) return;

  const slotsEditable = !!panelEl._slotsEditable;
  const skillsEditable = !!panelEl._skillsEditable;
  const slots = panelEl._slotState.slots || [];
  const skills = panelEl._skillsState || [];

  // Clamp a stale active pointer (e.g. after a delete).
  const act = panelEl._promptActive;
  if (act) {
    if (act.kind === 'slot' && act.idx >= slots.length) panelEl._promptActive = null;
    else if (act.kind === 'skill' && act.idx >= skills.length) panelEl._promptActive = null;
  }
  const isOpen = !!panelEl._promptActive;

  list.innerHTML = '';

  // Slot-load error banner (skills failures fall back to an empty list, as before).
  if (panelEl._slotsLoadError) {
    const err = document.createElement('div');
    err.style.cssText = 'font-size:12px;color:var(--danger);padding:8px;';
    err.textContent = panelEl._slotsLoadError;
    list.appendChild(err);
  }

  // Nothing to show and nothing to add → plain message.
  if (!slots.length && !skills.length && !slotsEditable && !skillsEditable) {
    const empty = document.createElement('div'); empty.style.cssText = 'font-size:12px;color:var(--fg-3);padding:8px 2px;';
    empty.textContent = 'This agent has no prompt slots or skills.';
    list.appendChild(empty);
    return;
  }

  // ── Squares carousel (tiles only — editor lives OUTSIDE this wrap) ───────────
  const wrap = document.createElement('div');
  wrap.className = 'prompt-squares-wrap' + (isOpen ? ' open' : '');

  const chevLeft = document.createElement('button'); chevLeft.type = 'button'; chevLeft.tabIndex = -1;
  chevLeft.className = 'prompt-carousel-chev left'; chevLeft.innerHTML = '&#10094;';
  chevLeft.setAttribute('aria-label', 'Scroll prompts left');

  const chevRight = document.createElement('button'); chevRight.type = 'button'; chevRight.tabIndex = -1;
  chevRight.className = 'prompt-carousel-chev right'; chevRight.innerHTML = '&#10095;';
  chevRight.setAttribute('aria-label', 'Scroll prompts right');

  const squares = document.createElement('div'); squares.className = 'prompt-squares';

  // + Add slot tile — first, like the New Agent square.
  if (slotsEditable) {
    const addSq = document.createElement('div');
    addSq.className = 'prompt-square-square prompt-square-add';
    addSq.innerHTML = `<div class="prompt-square-icon">${icon('plus', { size: '18px' })}</div><div class="prompt-square-name">Add slot</div>`;
    addSq.addEventListener('click', () => {
      const baseId = 'slot_' + (panelEl._slotState.slots.length + 1);
      panelEl._slotState.slots.push({ slot_name: baseId, content: '', order: panelEl._slotState.slots.length + 1 });
      panelEl._promptActive = { kind: 'slot', idx: panelEl._slotState.slots.length - 1 };
      _renderPromptCarousel(panelEl, agent);
      panelEl._slotsOnChange();
    });
    squares.appendChild(addSq);
  }

  // Slot tiles.
  slots.forEach((slot, idx) => {
    const slotId = slot.slot_name || slot.id || `slot-${idx}`;
    const isOverridden = panelEl._slotState.overrides && slotId in panelEl._slotState.overrides;
    const isActive = !!panelEl._promptActive && panelEl._promptActive.kind === 'slot' && panelEl._promptActive.idx === idx;

    const sq = document.createElement('div');
    sq.className = 'prompt-square-square' + (isActive ? ' activated' : '');

    const ico = document.createElement('div');
    ico.className = `prompt-square-icon ${_slotColor(slotId)}`;
    ico.innerHTML = icon(_slotIcon(slotId), { size: '18px' });
    sq.appendChild(ico);

    const nm = document.createElement('div');
    nm.className = 'prompt-square-name';
    nm.textContent = slotId.replace(/_/g, ' ');
    sq.appendChild(nm);

    if (slot.locked && slotsEditable) {
      const b = document.createElement('span'); b.className = 'prompt-square-badge locked'; b.textContent = '🔒'; sq.appendChild(b);
    } else if (isOverridden) {
      const b = document.createElement('span'); b.className = 'prompt-square-badge override'; b.textContent = '✎'; sq.appendChild(b);
    }

    sq.addEventListener('click', () => {
      panelEl._promptActive = isActive ? null : { kind: 'slot', idx };
      _renderPromptCarousel(panelEl, agent);
    });
    squares.appendChild(sq);
  });

  // Skill tiles — same strip, distinct look (book icon + mode badge).
  skills.forEach((skill, idx) => {
    const isActive = !!panelEl._promptActive && panelEl._promptActive.kind === 'skill' && panelEl._promptActive.idx === idx;

    const sq = document.createElement('div');
    sq.className = 'skill-square-square' + (isActive ? ' activated' : '');

    const ico = document.createElement('div'); ico.className = 'skill-square-icon color-purple';
    ico.innerHTML = icon('book-open', { size: '18px' });
    sq.appendChild(ico);

    const nm = document.createElement('div'); nm.className = 'skill-square-name';
    nm.textContent = skill.name || 'unnamed';
    sq.appendChild(nm);

    const b = document.createElement('span'); b.className = 'skill-square-mode';
    b.textContent = skill.mode === 'always_on' ? 'A' : 'S';
    b.title = skill.mode === 'always_on' ? 'Always on' : 'Selectable';
    sq.appendChild(b);

    sq.addEventListener('click', () => {
      panelEl._promptActive = isActive ? null : { kind: 'skill', idx };
      _renderPromptCarousel(panelEl, agent);
    });
    squares.appendChild(sq);
  });

  // + Add skill tile — last.
  if (skillsEditable) {
    const addSq = document.createElement('div');
    addSq.className = 'skill-square-square skill-square-add';
    addSq.innerHTML = `<div class="skill-square-icon color-purple">${icon('plus', { size: '18px' })}</div><div class="skill-square-name">Add</div>`;
    addSq.addEventListener('click', () => {
      panelEl._skillsState.push({ name: '', description: '', body: '', mode: 'selectable', enabled: true });
      panelEl._promptActive = { kind: 'skill', idx: panelEl._skillsState.length - 1 };
      _renderPromptCarousel(panelEl, agent);
      panelEl._skillsOnChange();
    });
    squares.appendChild(addSq);
  }

  wrap.appendChild(chevLeft); wrap.appendChild(squares); wrap.appendChild(chevRight);
  list.appendChild(wrap);

  // ── Shared detail area — SIBLING of the wrap (keeps chevrons off the editor) ──
  if (panelEl._promptActive) {
    const detail = panelEl._promptActive.kind === 'slot'
      ? _buildSlotDetail(panelEl, agent, panelEl._promptActive.idx)
      : _buildSkillDetail(panelEl, agent, panelEl._promptActive.idx);
    if (detail) {
      list.appendChild(detail);
      _fillPromptDetailViewport(detail);
    } else panelEl._promptActive = null;
  }

  if (isOpen) _wirePromptCarousel(wrap);
}

// Size the selected prompt viewer to the visible space below its carousel.
function _fillPromptDetailViewport(detail) {
  requestAnimationFrame(() => {
    if (!detail.isConnected) return;
    const gutter = 24;
    const available = Math.max(240, window.innerHeight - detail.getBoundingClientRect().top - gutter);
    detail.style.height = `${available}px`;
  });
}

// ── Slot editor ───────────────────────────────────────────────────────────────

function _buildSlotDetail(panelEl, agent, idx) {
  const slotsEditable = !!panelEl._slotsEditable;
  const slots = panelEl._slotState.slots || [];
  const slot = slots[idx];
  if (!slot) return null;

  const slotId = slot.slot_name || slot.id || `slot-${idx}`;
  const isOverridden = panelEl._slotState.overrides && slotId in panelEl._slotState.overrides;
  const content = isOverridden ? panelEl._slotState.overrides[slotId] : (slot.override_content != null ? slot.override_content : slot.content) || '';
  const canEdit = slotsEditable || !slot.locked; // members may override unlocked slots

  const detail = document.createElement('div'); detail.className = 'prompt-detail-area';

  const topBar = document.createElement('div'); topBar.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:8px;';
  const icoS = document.createElement('div'); icoS.className = `prompt-square-icon ${_slotColor(slotId)}`;
  icoS.style.cssText = 'width:28px;height:28px;font-size:14px;';
  icoS.innerHTML = icon(_slotIcon(slotId), { size: '16px' });
  topBar.appendChild(icoS);

  const lbl = document.createElement('span'); lbl.style.cssText = 'font-size:13px;font-weight:600;color:var(--fg-1);';
  lbl.textContent = slotId.replace(/_/g, ' '); topBar.appendChild(lbl);

  if (slot.locked && slotsEditable) {
    const b = document.createElement('span'); b.className = 'agents-field-label'; b.style.cssText = 'font-size:10px;color:var(--accent);border:1px solid var(--accent-soft);border-radius:3px;padding:1px 6px;margin:0;';
    b.textContent = 'Admin lock'; topBar.appendChild(b);
  }
  if (isOverridden) {
    const b = document.createElement('span'); b.className = 'agents-field-label'; b.style.cssText = 'font-size:10px;color:var(--warning);border:1px solid var(--warning);border-radius:3px;padding:1px 6px;margin:0;';
    b.textContent = 'Override'; topBar.appendChild(b);
  }
  if (slot.order != null) {
    const b = document.createElement('span'); b.style.cssText = 'font-size:10px;color:var(--fg-3);';
    b.textContent = `#${slot.order}`; topBar.appendChild(b);
  }

  // Autosave tick (pushed to the right edge, ahead of the close button).
  panelEl._slotsCheck.style.marginLeft = 'auto';
  topBar.appendChild(panelEl._slotsCheck);

  // Close button — collapses the editor and returns to the strip. NOT a delete.
  const closeBtn = document.createElement('button'); closeBtn.type = 'button'; closeBtn.textContent = '×'; closeBtn.title = 'Close';
  closeBtn.style.cssText = 'width:24px;height:24px;border:none;border-radius:6px;background:none;color:var(--fg-3);font-size:16px;cursor:pointer;';
  closeBtn.addEventListener('mouseenter', () => { closeBtn.style.background = 'var(--bg-2)'; closeBtn.style.color = 'var(--fg-1)'; });
  closeBtn.addEventListener('mouseleave', () => { closeBtn.style.background = 'none'; closeBtn.style.color = 'var(--fg-3)'; });
  closeBtn.addEventListener('click', () => {
    panelEl._promptActive = null;
    _renderPromptCarousel(panelEl, agent);
  });
  topBar.appendChild(closeBtn);
  detail.appendChild(topBar);

  const ta = document.createElement('textarea'); ta.className = 'agents-textarea prompt-detail-text';
  ta.style.cssText = 'width:100%;font-size:12px;font-family:monospace;line-height:1.4;';
  ta.value = content;
  if (!canEdit) ta.readOnly = true;
  ta.addEventListener('input', () => {
    if (slotsEditable && !slot.locked) {
      if (!panelEl._slotState.slots[idx]) return;
      panelEl._slotState.slots[idx] = { ...panelEl._slotState.slots[idx], content: ta.value };
    } else if (!slotsEditable && !slot.locked) {
      panelEl._slotState.overrides[slotId] = ta.value;
    }
    panelEl._slotsOnChange();
  });
  detail.appendChild(ta);

  return detail;
}

// ── Skill editor ──────────────────────────────────────────────────────────────

function _buildSkillDetail(panelEl, agent, idx) {
  const isEditable = !!panelEl._skillsEditable;
  const skills = panelEl._skillsState || [];
  const skill = skills[idx];
  if (!skill) return null;

  const detail = document.createElement('div'); detail.className = 'skill-detail-area';

  // Name row.
  const nmRow = document.createElement('div'); nmRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:8px;';
  const icoS = document.createElement('div'); icoS.className = 'skill-square-icon color-purple';
  icoS.style.cssText = 'width:28px;height:28px;font-size:14px;';
  icoS.innerHTML = icon('book-open', { size: '16px' });
  nmRow.appendChild(icoS);

  const nameInput = document.createElement('input'); nameInput.type = 'text'; nameInput.value = skill.name;
  nameInput.placeholder = 'skill-name'; nameInput.disabled = !isEditable;
  nameInput.style.cssText = 'flex:1;min-width:140px;padding:5px 8px;border:1px solid var(--border-soft);border-radius:6px;background:var(--bg-0);color:var(--fg-1);font-size:13px;font-weight:600;';
  nameInput.addEventListener('input', () => { skill.name = nameInput.value; panelEl._skillsOnChange(); });
  nmRow.appendChild(nameInput);

  // Autosave tick.
  nmRow.appendChild(panelEl._skillsCheck);

  if (isEditable) {
    const del = document.createElement('button'); del.type = 'button'; del.textContent = '×';
    del.title = 'Delete skill';
    del.style.cssText = 'width:24px;height:24px;border:none;border-radius:6px;background:none;color:var(--fg-3);font-size:16px;cursor:pointer;';
    del.addEventListener('mouseenter', () => { del.style.background = 'var(--danger)'; del.style.color = '#fff'; });
    del.addEventListener('mouseleave', () => { del.style.background = 'none'; del.style.color = 'var(--fg-3)'; });
    del.addEventListener('click', () => {
      panelEl._skillsState.splice(idx, 1);
      panelEl._promptActive = null;
      _renderPromptCarousel(panelEl, agent);
      panelEl._skillsOnChange();
    });
    nmRow.appendChild(del);
  } else {
    // Close button for read-only viewers (no delete affordance).
    const closeBtn = document.createElement('button'); closeBtn.type = 'button'; closeBtn.textContent = '×'; closeBtn.title = 'Close';
    closeBtn.style.cssText = 'width:24px;height:24px;border:none;border-radius:6px;background:none;color:var(--fg-3);font-size:16px;cursor:pointer;';
    closeBtn.addEventListener('mouseenter', () => { closeBtn.style.background = 'var(--bg-2)'; closeBtn.style.color = 'var(--fg-1)'; });
    closeBtn.addEventListener('mouseleave', () => { closeBtn.style.background = 'none'; closeBtn.style.color = 'var(--fg-3)'; });
    closeBtn.addEventListener('click', () => {
      panelEl._promptActive = null;
      _renderPromptCarousel(panelEl, agent);
    });
    nmRow.appendChild(closeBtn);
  }
  detail.appendChild(nmRow);

  // Toggles row.
  const toggles = document.createElement('div'); toggles.style.cssText = 'display:flex;align-items:center;gap:16px;margin-bottom:8px;';
  const alwaysWrap = document.createElement('label');
  alwaysWrap.style.cssText = 'display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--fg-2);cursor:pointer;';
  const alwaysCb = document.createElement('input'); alwaysCb.type = 'checkbox'; alwaysCb.checked = skill.mode === 'always_on';
  alwaysCb.disabled = !isEditable;
  const alwaysTxt = document.createElement('span');
  const setModeLabel = () => { alwaysTxt.textContent = alwaysCb.checked ? 'Always on' : 'Selectable'; };
  setModeLabel();
  alwaysCb.addEventListener('change', () => { skill.mode = alwaysCb.checked ? 'always_on' : 'selectable'; setModeLabel(); panelEl._skillsOnChange(); });
  alwaysWrap.appendChild(alwaysCb); alwaysWrap.appendChild(alwaysTxt);
  alwaysWrap.title = 'Checked = body always in the prompt. Unchecked = agent loads it on demand via load_skill.';
  toggles.appendChild(alwaysWrap);

  const enWrap = document.createElement('label');
  enWrap.style.cssText = 'display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--fg-3);cursor:pointer;';
  const enCb = document.createElement('input'); enCb.type = 'checkbox'; enCb.checked = skill.enabled !== false;
  enCb.disabled = !isEditable;
  enCb.addEventListener('change', () => { skill.enabled = enCb.checked; panelEl._skillsOnChange(); });
  enWrap.appendChild(enCb); enWrap.appendChild(document.createTextNode('Enabled'));
  toggles.appendChild(enWrap);
  detail.appendChild(toggles);

  // Description field.
  const descInput = document.createElement('textarea'); descInput.className = 'agents-textarea';
  descInput.rows = 1; descInput.value = skill.description; descInput.placeholder = 'When to use this skill (one-liner)';
  descInput.disabled = !isEditable;
  descInput.style.cssText = 'width:100%;margin-top:4px;font-size:12px;font-family:monospace;line-height:1.4;';
  descInput.addEventListener('input', () => { skill.description = descInput.value; panelEl._skillsOnChange(); });
  detail.appendChild(descInput);

  // Body field.
  const bodyInput = document.createElement('textarea'); bodyInput.className = 'agents-textarea prompt-detail-text';
  bodyInput.rows = 4; bodyInput.value = skill.body; bodyInput.placeholder = 'Full skill body (system-prompt content)';
  bodyInput.disabled = !isEditable;
  bodyInput.style.cssText = 'width:100%;margin-top:4px;font-size:12px;font-family:monospace;line-height:1.4;';
  bodyInput.addEventListener('input', () => { skill.body = bodyInput.value; panelEl._skillsOnChange(); });
  detail.appendChild(bodyInput);

  return detail;
}

// ╔═╗ CAROUSEL-WIRING PATTERN (3rd of 4 copies — sisters: _wireSquaresCarousel + ════════════╗
// ║ _wireTabCarousel in view.js, _wireSkillsCarousel in claude-skills.js).                ║
// ║ Native scroll + chevron affordance for the unified prompt/skill squares strip.      ║
// ║ If you fix scroll or affordance logic, update ALL FOUR copies.                      ║
// ╚══════════════════════════════════════════════════════════════════════════════════════════╝
function _wirePromptCarousel(wrap) {
  const scroller = wrap.querySelector('.prompt-squares');
  if (!scroller) return;
  const chevLeft  = wrap.querySelector('.prompt-carousel-chev.left');
  const chevRight = wrap.querySelector('.prompt-carousel-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd   = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };

  scroller.addEventListener('scroll', updateAffordances, { passive: true });
    applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  setTimeout(updateAffordances, 120);
  if (typeof ResizeObserver !== 'undefined') { new ResizeObserver(updateAffordances).observe(scroller); }

  const page = () => Math.max(scroller.clientWidth * 0.7, 100);
  if (chevLeft)  chevLeft.addEventListener('click',  e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left:  page(), behavior: 'smooth' }); });
}

// ── Saving ──────────────────────────────────────────────────────────────────

async function _saveSlotsNow(agent, panelEl, indicator) {
  const slots = panelEl._slotState.slots || [];
  // Normalise: send only non-empty slots
  const cleanSlots = slots.filter(s => (s.content || '').trim() || (s.slot_name || s.id));
  const overrides = panelEl._slotState.overrides || {};
  const resetKeys = [...(panelEl._slotState.resetOverridesFor || [])];
  panelEl._slotState.resetOverridesFor = new Set();

  try {
    const res = await fetch(`/api/v1/agents/${agent.id}/my-prompts`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: app.currentUserId,
        slots: cleanSlots,
        overrides: Object.keys(overrides).length ? overrides : undefined,
        reset_overrides: resetKeys.length ? resetKeys : undefined,
      }),
    });
    if (res.ok) { _flashSaved(indicator, true); }
    else { const d = await res.json(); _flashSaved(indicator, false, d.detail || 'Save failed'); }
  } catch (e) { _flashSaved(indicator, false, e.message); }
}

async function _saveSkillsNow(agent, panelEl, indicator) {
  const skills = panelEl._skillsState || [];
  try {
    const headers = { 'Content-Type': 'application/json', ...authHeaders() };
    const res = await fetch(`/api/v1/agents/${agent.id}/skills?user_id=${encodeURIComponent(app.currentUserId)}`, {
      method: 'PUT', headers, body: JSON.stringify({ skills }),
    });
    if (res.ok) { _flashSaved(indicator, true); }
    else { const d = await res.json(); _flashSaved(indicator, false, d.detail || 'Save failed'); }
  } catch (e) { _flashSaved(indicator, false, e.message); }
}

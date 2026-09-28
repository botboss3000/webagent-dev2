'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// CSS variables resolve inside inline styles — use e.g. 'var(--accent)' or
// 'rgba(var(--brand-rgb), 0.16)'. Never hard-code a hex/rgb colour literal here.

/**
 * Agents — Local Claude Code: the SKILLS tab.
 *
 * A second tab on the (otherwise tab-less) Claude Code agent card. It MIRRORS the
 * normal agent's Prompts tab (ui/main-panel/agents/js/tab-prompts.js): a single
 * horizontal strip of skill SQUARES in a drag/chevron CAROUSEL, and clicking a
 * square opens one editor BELOW the strip. But here the "skills" are the REAL
 * SKILL.md files the local `claude` discovers — personal (~/.claude/skills) and the
 * agent's working folder (<folder>/.claude/skills) — so each editor also carries a
 * rendered Markdown PREVIEW of the file.
 *
 * Reuses the pre-staged `.skills-squares*` / `.skill-square-*` CSS in
 * ui/main-panel/agents/agents.css. Backend: app/api/claude_skills.py →
 * plugins/engines/claude_code/claude_code_skills.py (admin-gated). The tab is mounted by
 * mountClaudeCardTabs in ui/main-panel/agents/js/claude-agent.js.
 *
 * REMOVE-WHEN: the Local Claude Code engine (claude_code) is dropped.
 */

import { icon } from '../../../shared/js/icons.js';
import { _esc, _makeAutosaveCheck, _flashSaved, _debounced } from './utils.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';

const _CC = '/api/v1/claude-code';
const _SRC_LABEL = { personal: 'Personal', project: 'Project' };

/** Entry point — render the Skills tab into `body` for a claude_code `agent`. */
export function renderClaudeSkills(body, agent) {
  const host = document.createElement('div'); host.className = 'claude-skills-host';

  const intro = document.createElement('div'); intro.className = 'claude-agent-intro';
  intro.textContent = "Skills the local Claude finds — your personal skills plus any in this "
    + "agent's project folder. Each is a SKILL.md you can edit and preview as Markdown.";
  host.appendChild(intro);

  const list = document.createElement('div'); list.dataset.role = 'skills-list';
  list.innerHTML = '<div class="agents-spinner" style="margin:12px auto;display:block;"></div>';
  host.appendChild(list);
  body.appendChild(host);

  const state = {
    skills: [],            // [{ source, slug, name, description, body, _new? }]
    dirs: {},              // { personal, project } absolute paths (for hints)
    loaded: false,
    error: '',
    active: null,          // index into skills, or null
    mode: 'edit',          // 'edit' | 'preview' for the open editor
    check: _makeAutosaveCheck(),
    _commit: null,         // flush the open editor's pending save (set by _buildEditor)
  };

  _load(agent, state, list);
}

// ── Data loading ───────────────────────────────────────────────────────────

async function _load(agent, state, list) {
  try {
    const r = await fetch(`${_CC}/skills?agent_id=${encodeURIComponent(agent.id)}`, { headers: authHeaders() });
    if (r.status === 403) {
      state.error = 'Skills are admin-only.';
    } else if (r.ok) {
      const d = await r.json();
      state.skills = (d.skills || []).map(s => ({
        source: s.source, slug: s.slug || '', name: s.name || '',
        description: s.description || '', body: s.body || '',
      }));
      state.dirs = d.dirs || {};
    } else {
      state.error = 'Couldn’t load skills.';
    }
  } catch (e) {
    state.error = 'Error: ' + e.message;
  }
  state.loaded = true;
  _render(agent, state, list);
}

// ── Carousel render ────────────────────────────────────────────────────────

function _render(agent, state, list) {
  if (!state.loaded) return;
  // Clamp a stale active pointer (after a delete).
  if (state.active != null && state.active >= state.skills.length) state.active = null;
  const isOpen = state.active != null;
  // No editor mounted (yet) → nothing to commit.
  state._commit = null;

  list.innerHTML = '';

  if (state.error) {
    const err = document.createElement('div');
    err.style.cssText = 'font-size:12px;color:var(--danger);padding:8px 2px;';
    err.textContent = state.error;
    list.appendChild(err);
    return;
  }

  const wrap = document.createElement('div');
  wrap.className = 'skills-squares-wrap' + (isOpen ? ' open' : '');

  const chevLeft = document.createElement('button'); chevLeft.type = 'button'; chevLeft.tabIndex = -1;
  chevLeft.className = 'skills-carousel-chev left'; chevLeft.innerHTML = '&#10094;';
  chevLeft.setAttribute('aria-label', 'Scroll skills left');
  const chevRight = document.createElement('button'); chevRight.type = 'button'; chevRight.tabIndex = -1;
  chevRight.className = 'skills-carousel-chev right'; chevRight.innerHTML = '&#10095;';
  chevRight.setAttribute('aria-label', 'Scroll skills right');

  const squares = document.createElement('div'); squares.className = 'skills-squares';

  state.skills.forEach((skill, idx) => {
    const isActive = state.active === idx;
    const sq = document.createElement('div');
    sq.className = 'skill-square-square' + (isActive ? ' activated' : '');

    const ico = document.createElement('div'); ico.className = 'skill-square-icon color-purple';
    ico.innerHTML = icon('book-open', { size: '18px' });
    sq.appendChild(ico);

    const nm = document.createElement('div'); nm.className = 'skill-square-name';
    nm.textContent = (skill.name || skill.slug || 'unnamed');
    sq.appendChild(nm);

    const b = document.createElement('span'); b.className = 'skill-square-mode';
    b.textContent = skill.source === 'project' ? 'F' : 'P';
    b.title = skill.source === 'project' ? 'Project folder' : 'Personal';
    sq.appendChild(b);

    sq.addEventListener('click', () => {
      if (state._commit) state._commit();       // commit the open editor's pending edit first
      state.active = isActive ? null : idx;
      state.mode = 'edit';
      _render(agent, state, list);
    });
    squares.appendChild(sq);
  });

  // + Add tiles — one per source, like the Prompts tab's "Add" square.
  ['personal', 'project'].forEach(source => {
    const addSq = document.createElement('div');
    addSq.className = 'skill-square-square skill-square-add';
    addSq.innerHTML = `<div class="skill-square-icon color-purple">${icon('plus', { size: '18px' })}</div>`
      + `<div class="skill-square-name">${source === 'project' ? 'Project' : 'Personal'}</div>`;
    addSq.title = `Add a ${_SRC_LABEL[source]} skill`;
    addSq.addEventListener('click', () => {
      if (state._commit) state._commit();
      state.skills.push({ source, slug: '', name: '', description: '', body: '', _new: true });
      state.active = state.skills.length - 1;
      state.mode = 'edit';
      _render(agent, state, list);
    });
    squares.appendChild(addSq);
  });

  wrap.appendChild(chevLeft); wrap.appendChild(squares); wrap.appendChild(chevRight);
  list.appendChild(wrap);

  if (isOpen) {
    const detail = _buildEditor(agent, state, list, state.active);
    if (detail) list.appendChild(detail);
    _wireSkillsCarousel(wrap);
  }
}

// ── Skill editor (with Markdown preview) ───────────────────────────────────

function _buildEditor(agent, state, list, idx) {
  const skill = state.skills[idx];
  if (!skill) return null;

  // Per-editor debounced save, bound to THIS index — so switching skills mid-edit
  // can never save edits to the wrong one. `state._commit` flushes any pending edit
  // before this editor unmounts (called by the square/add/close handlers).
  let dirty = false;
  const scheduleSave = _debounced(() => { dirty = false; _save(agent, state, idx); }, 800);
  const schedule = () => { dirty = true; scheduleSave(); };
  state._commit = () => { if (dirty) scheduleSave.flush(); };

  const detail = document.createElement('div'); detail.className = 'skill-detail-area';

  // Top bar: icon · Edit/Preview toggle · source badge · save tick · delete · close
  const top = document.createElement('div');
  top.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:8px;';
  const ico = document.createElement('div'); ico.className = 'skill-square-icon color-purple';
  ico.style.cssText = 'width:28px;height:28px;font-size:14px;';
  ico.innerHTML = icon('book-open', { size: '16px' });
  top.appendChild(ico);

  const seg = document.createElement('div'); seg.className = 'claude-skill-seg';
  const editBtn = document.createElement('button'); editBtn.type = 'button'; editBtn.textContent = 'Edit';
  const prevBtn = document.createElement('button'); prevBtn.type = 'button'; prevBtn.textContent = 'Preview';
  editBtn.classList.toggle('active', state.mode !== 'preview');
  prevBtn.classList.toggle('active', state.mode === 'preview');
  seg.appendChild(editBtn); seg.appendChild(prevBtn);
  top.appendChild(seg);

  const src = document.createElement('span'); src.className = 'claude-skill-src';
  src.textContent = _SRC_LABEL[skill.source] || skill.source;
  top.appendChild(src);

  state.check.style.marginLeft = 'auto';
  top.appendChild(state.check);

  const del = document.createElement('button'); del.type = 'button'; del.className = 'claude-skill-iconbtn';
  del.title = 'Delete skill'; del.innerHTML = icon('trash-2', { size: '14px' });
  del.addEventListener('mouseenter', () => { del.style.color = 'var(--danger)'; });
  del.addEventListener('mouseleave', () => { del.style.color = 'var(--fg-3)'; });
  del.addEventListener('click', () => _deleteSkill(agent, state, list, idx));
  top.appendChild(del);

  const close = document.createElement('button'); close.type = 'button'; close.className = 'claude-skill-iconbtn';
  close.title = 'Close'; close.textContent = '×'; close.style.fontSize = '16px';
  close.addEventListener('click', () => {
    if (state._commit) state._commit();
    // Discard an untouched brand-new skill rather than leaving an empty tile.
    if (skill._new && !(skill.name || '').trim()) state.skills.splice(idx, 1);
    state.active = null;
    _render(agent, state, list);
  });
  top.appendChild(close);
  detail.appendChild(top);

  // ── Edit body ──
  const editWrap = document.createElement('div');

  const nameInput = document.createElement('input'); nameInput.type = 'text';
  nameInput.className = 'claude-skill-name'; nameInput.value = skill.name;
  nameInput.placeholder = 'skill-name';
  nameInput.addEventListener('input', () => { skill.name = nameInput.value; schedule(); });
  editWrap.appendChild(nameInput);

  const descInput = document.createElement('textarea'); descInput.className = 'agents-textarea';
  descInput.rows = 2; descInput.value = skill.description;
  descInput.placeholder = 'When to use this skill (one-liner shown to Claude)';
  descInput.style.cssText = 'width:100%;margin-top:6px;font-size:12px;font-family:monospace;line-height:1.4;';
  descInput.addEventListener('input', () => { skill.description = descInput.value; schedule(); });
  editWrap.appendChild(descInput);

  const bodyInput = document.createElement('textarea'); bodyInput.className = 'agents-textarea';
  bodyInput.rows = 9; bodyInput.value = skill.body;
  bodyInput.placeholder = 'Full skill body (Markdown — the SKILL.md content)';
  bodyInput.style.cssText = 'width:100%;margin-top:6px;font-size:12px;font-family:monospace;line-height:1.45;';
  bodyInput.addEventListener('input', () => { skill.body = bodyInput.value; schedule(); });
  editWrap.appendChild(bodyInput);

  // Where the file lives — helps a non-coder know what's being written.
  const path = _skillPath(state, skill);
  if (path) {
    const hint = document.createElement('div'); hint.className = 'claude-skill-path';
    hint.textContent = path;
    editWrap.appendChild(hint);
  }

  // ── Preview body ──
  const preview = document.createElement('div'); preview.className = 'claude-skill-md';
  const repaintPreview = () => { preview.innerHTML = _renderMd(skill.name, skill.description, skill.body); };

  const applyMode = () => {
    const previewing = state.mode === 'preview';
    editBtn.classList.toggle('active', !previewing);
    prevBtn.classList.toggle('active', previewing);
    editWrap.style.display = previewing ? 'none' : '';
    preview.style.display = previewing ? '' : 'none';
    if (previewing) repaintPreview();
  };
  editBtn.addEventListener('click', () => { state.mode = 'edit'; applyMode(); });
  prevBtn.addEventListener('click', () => { if (state._commit) state._commit(); state.mode = 'preview'; applyMode(); });

  detail.appendChild(editWrap);
  detail.appendChild(preview);
  applyMode();
  return detail;
}

/** Best-effort target path string for the editor hint. */
function _skillPath(state, skill) {
  const base = state.dirs && state.dirs[skill.source];
  if (!base) return '';
  const slug = skill.slug || _clientSlug(skill.name);
  if (!slug) return base.replace(/[\\/]+$/, '') + '  (pick a name to create)';
  const sep = base.includes('\\') ? '\\' : '/';
  return [base.replace(/[\\/]+$/, ''), slug, 'SKILL.md'].join(sep);
}

/** Mirror of the backend slugify, only for the live path hint (server is authoritative). */
function _clientSlug(name) {
  const s = (name || '').trim().toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[-._]+|[-._]+$/g, '').slice(0, 64);
  return s || '';
}

/** Compose a small SKILL.md preview: name as a heading, description as a muted
 *  subtitle, body rendered as Markdown (sanitised). */
function _renderMd(name, description, body) {
  let bodyHtml;
  try {
    bodyHtml = (window.marked && typeof window.marked.parse === 'function')
      ? window.marked.parse(body || '', { gfm: true, breaks: false })
      : null;
  } catch (_) { bodyHtml = null; }
  if (bodyHtml == null) {
    bodyHtml = '<pre style="white-space:pre-wrap;">' + _esc(body || '') + '</pre>';
  } else if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    bodyHtml = window.DOMPurify.sanitize(bodyHtml);
  }
  const head = `<h1>${_esc(name || 'Untitled skill')}</h1>`
    + (description ? `<div class="claude-skill-desc">${_esc(description)}</div>` : '');
  return head + bodyHtml;
}

// ── Save / delete ──────────────────────────────────────────────────────────

async function _save(agent, state, idx) {
  const skill = state.skills[idx];
  if (!skill) return;
  const name = (skill.name || '').trim();
  if (!name) return;                          // need a name before writing a file
  try {
    const r = await fetch(`${_CC}/skills`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        agent_id: agent.id, source: skill.source, slug: skill.slug || null,
        name, description: skill.description || '', body: skill.body || '',
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      if (d.skill && d.skill.slug) { skill.slug = d.skill.slug; skill._new = false; }
      _flashSaved(state.check, true);
    } else {
      _flashSaved(state.check, false, d.detail || 'Save failed');
    }
  } catch (e) {
    _flashSaved(state.check, false, e.message);
  }
}

async function _deleteSkill(agent, state, list, idx) {
  const skill = state.skills[idx];
  if (!skill) return;
  // Unsaved new skill → just drop it locally.
  if (skill._new && !skill.slug) {
    state.skills.splice(idx, 1);
    state.active = null;
    _render(agent, state, list);
    return;
  }
  if (!confirm(`Delete the "${skill.name || skill.slug}" skill? This removes its folder on disk.`)) return;
  try {
    const qs = `agent_id=${encodeURIComponent(agent.id)}&source=${encodeURIComponent(skill.source)}&slug=${encodeURIComponent(skill.slug)}`;
    const r = await fetch(`${_CC}/skills?${qs}`, { method: 'DELETE', headers: authHeaders() });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      alert(d.detail || 'Delete failed.');
      return;
    }
  } catch (e) {
    alert('Delete failed: ' + e.message);
    return;
  }
  state.skills.splice(idx, 1);
  state.active = null;
  _render(agent, state, list);
}

// ╔═╗ CAROUSEL-WIRING PATTERN (4th copy — sisters: _wireSquaresCarousel + ════════╗
// ║ _wireTabCarousel in view.js, _wirePromptCarousel in tab-prompts.js).          ║
// ║ Native scroll + chevron affordance. If you fix scroll or affordance logic,    ║
// ║ update ALL FOUR copies.                                                       ║
// ╚════════════════════════════════════════════════════════════════════════════════╝
function _wireSkillsCarousel(wrap) {
  const scroller = wrap.querySelector('.skills-squares');
  if (!scroller) return;
  const chevLeft  = wrap.querySelector('.skills-carousel-chev.left');
  const chevRight = wrap.querySelector('.skills-carousel-chev.right');

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

'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agents — Config tab.
 *
 * LAYOUT: this tab mirrors the App Settings "standard config-page layout" (see
 * docs/claude/ui-guidance.md → "The standard config-page layout" + "Toggle-list").
 * Every section is a title-only `.ac-category-group` (an accent Lucide icon + a
 * title, no explainer paragraph) over ONE flush `.ac-list` table of rows — option
 * settings (toggle / dropdown / number) sit inline on the right of a row in the
 * `.ac-config-control` slot. The few genuine free-text fields
 * (Name / Description / Chat Messages) stay as fields (NOT rows — per the
 * "when NOT to use rows" rule) under the same titled headings.
 *
 * SAVES: every control confirms with the ONE shared on-top save overlay — a
 * spinner over the touched control → green ✓, or an orange ⚠ that reverts — the
 * same `_markSaving`/`_flashSaveCheck` (imported here ALIASED as `_ovMarkSaving`/
 * `_ovFlashCheck` — see the naming-clash note on the import below) the ability
 * tables + model table use. Saves are SILENT (`_putAgentField {silent:true}`) so a
 * single edit no longer tears down/re-mounts the open card under the user (the
 * model table already did this). Local caches still update inside `_putAgentField`.
 */

import { app } from '../../../shared/js/state.js';
import { icon } from '../../../shared/js/icons.js';
import { authHeaders, isAnonGuest } from '../../../shared/js/left-login.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../shared/js/delete-control.js';
import { reconcileSavedModelRows } from '../../../shared/js/model-roster-reconcile.js';
import { NODE_PANEL_INFO } from '../agent-loop/js/loop-node-data.js';
import {
  _agents, _isMockAgent, MOCK_AGENT_ID,
  _userIsAdmin, _extendLlmToAgents,
  _binView, _clearExpanded, _getAgentConfigPage, _setAgentConfigPage,
} from '../js/state.js';
import { renderVoiceConversationSettings } from '../js/voice-conversation-settings.js';
import {
  _esc, _btn,
  _debounced, _putAgentField, _triggerKeyPlaceholder,
  _renderAgentIcon, _iconColor,
} from '../js/utils.js';
import { _mockRandomIcon, _postNewAgent } from '../js/mock-agent.js';
import { _openIconPicker } from '../js/icon-picker.js';
import { _loadAgents } from '../js/data.js';
import { mountModelTable } from '../../../shared/js/model-table.js?v=15';
import { initModelAnalytics } from '../../app/settings/agent-settings/model-analytics.js?v=7';
import { apiPath } from '../../../shared/js/config.js';
import { copyText } from '../../../shared/js/clipboard.js';
import { showQrPopup } from '../../../shared/js/qr-popup.js';

import { DEFAULT_WEBAGENT_SUGGESTION_PROMPTS } from '../../../shared/js/app-prompts.js';
// NOTE — naming clash: `_markSaving` exists in BOTH ./utils.js (clears the legacy
// inline `.agents-autosave-check` tick) AND shared/js/dom-utils.js (the on-top
// `.ac-save-overlay`). This tab uses the OVERLAY one, imported aliased so it can't
// collide with the utils version; the legacy inline-tick path is no longer used here.
import { _wrapNumberStepper, _markSaving as _ovMarkSaving, _flashSaveCheck as _ovFlashCheck, openFloatingMenu, buildGateParamRowsHtml, wireGateParamRows } from '../../../shared/js/dom-utils.js';
import { renderAgentIdentitySettings } from '../js/identity-settings.js';
import { AGENT_CONFIG_PAGES, createAgentConfigPageController } from './index.js';

const RECENT_CONFIG_TOPICS_LIMIT = 3;
const RECENT_CONFIG_TOPIC_IDS = new Set(AGENT_CONFIG_PAGES.map((page) => page.id));
const ADVANCED_CONFIG_TOPIC_IDS = new Set(
  AGENT_CONFIG_PAGES.filter((page) => page.advanced).map((page) => page.id),
);

// Gate mode picker — the checkbox glyphs for the Gate toggle's floating panel.
// The panel reuses the ability rows' `openFloatingMenu` component verbatim; only
// the leading icon differs (a checkbox square, checked vs empty) so each mode
// entry reads as a checkbox. Inline SVG (like the ability menu icons) so the
// panel paints without a lucide pass. (GATES-SCOPE-POPOVER.)
const GATE_BOX_ON_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="m8 12 3 3 5-6"/></svg>';
const GATE_BOX_OFF_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/></svg>';

function _recentConfigTopicsKey(agent) {
  const userId = app.currentUserId || 'anonymous';
  return `webagent.agent-config.recent.v1:${userId}:${agent.id}`;
}

function _readRecentConfigTopicIds(agent) {
  try {
    const ids = JSON.parse(localStorage.getItem(_recentConfigTopicsKey(agent)) || '[]');
    return Array.isArray(ids) ? ids.filter(id => RECENT_CONFIG_TOPIC_IDS.has(id)) : [];
  } catch (_) {
    return [];
  }
}

function _writeRecentConfigTopicIds(agent, ids) {
  try {
    localStorage.setItem(_recentConfigTopicsKey(agent), JSON.stringify(ids.slice(0, RECENT_CONFIG_TOPICS_LIMIT)));
  } catch (_) { /* the local cache is optional */ }
}

function _recentConfigTopicsUrl(agent) {
  return `/api/v1/agents/${encodeURIComponent(agent.id)}/config/recent-topics?user_id=${encodeURIComponent(app.currentUserId || '')}`;
}

function _createRecentConfigTopicsGroup(body) {
  const group = document.createElement('section');
  group.className = 'agent-config-settings-group agent-config-recent-group';
  group.setAttribute('aria-label', 'Recent agent configuration');
  const label = document.createElement('div');
  label.className = 'agent-config-settings-group-label';
  label.innerHTML = `${icon('history', { size: '14px' })}<span>Recent</span>`;
  const host = document.createElement('div');
  host.className = 'agent-config-topics';
  group.append(label, host);
  body.appendChild(group);

  return {
    render(ids, topicById) {
      const entries = ids.map(id => topicById.get(id)).filter(Boolean).slice(0, RECENT_CONFIG_TOPICS_LIMIT);
      group.hidden = !entries.length;
      host.replaceChildren();
      for (const entry of entries) {
        const row = document.createElement('section');
        row.className = 'agent-config-topic agent-config-recent-topic';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'agent-config-topic-heading';
        button.innerHTML =
          `<span class="agent-config-topic-icon" aria-hidden="true">${icon(entry.iconName, { size: '18px' })}</span>` +
          `<span class="agent-config-topic-copy"><span class="agent-config-topic-title"></span><span class="agent-config-topic-description"></span></span>` +
          `<span class="agent-config-topic-chevron" aria-hidden="true">${icon('chevron-right', { size: '18px' })}</span>`;
        button.querySelector('.agent-config-topic-title').textContent = entry.title;
        button.querySelector('.agent-config-topic-description').textContent = entry.description;
        button.addEventListener('click', () => {
          entry.open();
          entry.content.parentElement?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        });
        row.appendChild(button);
        host.appendChild(row);
      }
    },
  };
}

/** Brief non-blocking confirmation for actions in the Agent Config tab. */
function _showConfigActionNotice(text, isError = false) {
  let wrap = document.getElementById('agent-config-action-notices');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'agent-config-action-notices';
    wrap.className = 'agent-config-action-notices';
    wrap.setAttribute('aria-live', 'polite');
    document.body.appendChild(wrap);
  }
  const notice = document.createElement('div');
  notice.className = 'agent-config-action-notice' + (isError ? ' is-error' : '');
  notice.innerHTML = icon(isError ? 'circle-alert' : 'check', { size: '14px' }) + '<span></span>';
  notice.querySelector('span').textContent = text;
  wrap.appendChild(notice);
  requestAnimationFrame(() => notice.classList.add('is-visible'));
  setTimeout(() => {
    notice.classList.remove('is-visible');
    setTimeout(() => {
      notice.remove();
      if (!wrap.children.length) wrap.remove();
    }, 180);
  }, 1400);
}

// ── Shared layout helpers (App-Settings standard config-page layout) ──────────

/** A title-only section: accent Lucide icon + title over a `.ac-category-body`.
 *  Returns the body element to fill (with an `.ac-list` and/or fields).
 *  When ``info`` is given, an info (help-circle) button appears after the title
 *  that opens a popover explaining the section. */
function _group(body, iconName, title, info) {
  const group = document.createElement('div');
  group.className = 'ac-category-group';
  const summary = document.createElement('div');
  summary.className = 'ac-category-summary';
  summary.style.cursor = 'default';   // these headings aren't collapsible
  summary.innerHTML =
    `<i data-lucide="${iconName}" class="lucide-icon" style="width:16px;height:16px;color:var(--accent);"></i>`;
  // Title + (optional info button) live in one left-anchored cluster so the
  // button hugs the heading text. A bare .ac-category-title has flex:1, which
  // would shove a sibling button to the far right edge of the row.
  const titleCluster = document.createElement('span');
  titleCluster.style.cssText = 'display:inline-flex;align-items:center;gap:6px;flex:1;min-width:0;';
  const titleEl = document.createElement('span');
  titleEl.className = 'ac-category-title';
  titleEl.style.flex = '0 0 auto';
  titleEl.textContent = title;
  titleCluster.appendChild(titleEl);
  summary.appendChild(titleCluster);
  if (info) {
    const infoBtn = document.createElement('button');
    infoBtn.type = 'button';
    infoBtn.setAttribute('aria-label', `About ${title}`);
    infoBtn.title = 'What is this?';
    infoBtn.style.cssText = 'background:none;border:none;cursor:pointer;color:var(--fg-2);padding:2px;display:inline-flex;align-items:center;border-radius:4px;transition:color .15s;';
    infoBtn.innerHTML = icon('help-circle', { size: '14px' });
    infoBtn.addEventListener('mouseenter', () => { infoBtn.style.color = 'var(--accent)'; });
    infoBtn.addEventListener('mouseleave', () => { infoBtn.style.color = 'var(--fg-2)'; });
    infoBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _acInfoPopover(infoBtn, title, info);
    });
    titleCluster.appendChild(infoBtn);
  }
  const gbody = document.createElement('div');
  gbody.className = 'ac-category-body';
  group.appendChild(summary);
  group.appendChild(gbody);
  body.appendChild(group);
  return gbody;
}

/** Top-level Agent Config topic. These are intentionally broader than `_group`:
 *  a topic owns every related sub-group, so the page reads as a short, ordered
 *  outline instead of a long run of peer headings. The open state is retained
 *  per agent for the life of the browser session. */
function _topic(body, agent, iconName, title, { description = '', open = false, surface = 'plain', recentId = '', onOpen = null, onNavigate = null } = {}) {
  const pageMode = typeof onNavigate === 'function';
  const key = `webagent.agent-config.${agent.id}.${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  const topicSurface = surface === 'card' ? 'card' : 'plain';
  let expanded = pageMode ? false : open;
  if (!pageMode) {
    try {
      const saved = sessionStorage.getItem(key);
      if (saved !== null) expanded = saved === '1';
    } catch (_) { /* storage is optional */ }
  }

  const topic = document.createElement('section');
  topic.className = 'agent-config-topic';
  if (expanded) topic.classList.add('is-open');

  const heading = document.createElement('button');
  heading.type = 'button';
  heading.className = 'agent-config-topic-heading';
  if (pageMode) {
    heading.setAttribute('aria-label', `Open ${title}`);
  } else {
    heading.setAttribute('aria-expanded', String(expanded));
  }
  heading.innerHTML =
    `<span class="agent-config-topic-icon" aria-hidden="true">${icon(iconName, { size: '18px' })}</span>` +
    `<span class="agent-config-topic-copy"><span class="agent-config-topic-title"></span><span class="agent-config-topic-description"></span></span>` +
    `<span class="agent-config-topic-chevron" aria-hidden="true">${icon('chevron-right', { size: '18px' })}</span>`;
  heading.querySelector('.agent-config-topic-title').textContent = title;
  heading.querySelector('.agent-config-topic-description').textContent = description;

  const content = document.createElement('div');
  content.className = `agent-config-topic-content agent-config-topic-content--${topicSurface}`;
  content.hidden = !expanded;

  const openTopic = () => {
    if (pageMode) {
      if (recentId) onOpen?.(recentId);
      onNavigate(recentId);
      return;
    }
    setExpanded(true, true);
  };
  const setExpanded = (next, remember = false) => {
    if (expanded === next) return;
    expanded = next;
    topic.classList.toggle('is-open', expanded);
    heading.setAttribute('aria-expanded', String(expanded));
    content.hidden = !expanded;
    try { sessionStorage.setItem(key, expanded ? '1' : '0'); } catch (_) { /* storage is optional */ }
    if (expanded && remember && recentId) onOpen?.(recentId);
  };
  heading.addEventListener('click', () => pageMode ? openTopic() : setExpanded(!expanded, true));

  topic.append(heading, content);
  body.appendChild(topic);
  // The Recent group needs to open the canonical page rather than duplicate controls.
  content._openAgentConfigTopic = openTopic;
  return content;
}

/** A flush option-row table inside a group body. */
function _cfgList(gbody) {
  const list = document.createElement('div');
  list.className = 'ac-list ac-config-list';
  gbody.appendChild(list);
  return list;
}

/** One option row: bold name + muted description on the left, an empty
 *  `.ac-config-control` slot on the right for the caller to fill (input / toggle
 *  / select). Returns the row, the control slot, and the description element. */
function _cfgRow(list, name, desc) {
  const row = document.createElement('div'); row.className = 'ac-ability-row';
  const label = document.createElement('span'); label.className = 'ac-ability-label';
  const nameEl = document.createElement('span'); nameEl.className = 'ac-ability-name';
  nameEl.textContent = name; label.appendChild(nameEl);
  let descEl = null;
  if (desc) {
    descEl = document.createElement('span'); descEl.className = 'ac-ability-desc';
    descEl.textContent = desc; label.appendChild(descEl);
  }
  const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
  row.appendChild(label); row.appendChild(ctrl);
  list.appendChild(row);
  return { row, ctrl, descEl };
}

/** Wire a number stepper whose ceiling is an app-global resume limit.
 *
 *  Keeps the up-arrow ACTIVE only while the field is below `limit`: at the
 *  ceiling the arrow locks (disabled) and a small `.ac-num-limit-tip` bubble
 *  reading "Global limit is N" is revealed on hover / keyboard focus. A disabled
 *  button can't be relied on for :hover in every engine, so the bubble is a REAL
 *  node inserted as the up-arrow's PREVIOUS SIBLING (inside the stepper wrapper)
 *  — it stays hoverable via the wrapper, and a structural assertion can prove
 *  the sibling order. Callers keep `input.max = limit` so stepping and typing are
 *  clamped too. */
function _wireGlobalLimitStepper(inputEl, wrap, limit) {
  const up = wrap.querySelector('.ac-num-up');
  const tip = document.createElement('span');
  tip.className = 'ac-num-limit-tip';
  tip.setAttribute('role', 'tooltip');
  tip.textContent = `Global limit is ${limit}`;
  if (up) wrap.insertBefore(tip, up); else wrap.appendChild(tip);
  const syncLock = () => {
    const v = parseInt(inputEl.value, 10);
    const atMax = !isNaN(v) && v >= limit;
    wrap.classList.toggle('ac-num-at-max', atMax);
    if (up) up.disabled = atMax;
  };
  inputEl.addEventListener('input', syncLock);
  inputEl.addEventListener('change', syncLock);
  syncLock();
  return wrap;
}

/** A genuine free-text field (NOT a row): a `.ac-label` (with an inline save-
 *  indicator slot) over a full-width `.ac-input` input/textarea. Wires debounced
 *  autosave through the on-top overlay (placed over the small indicator slot so it
 *  never covers the text). `opts.onSave(value)` must return a Promise<boolean>. */
function _cfgField(gbody, { label, field, value, multiline, rows, placeholder, hint, readonly, onSave }) {
  const wrap = document.createElement('div'); wrap.className = 'ac-cfg-field';
  const lbl = document.createElement('label'); lbl.className = 'ac-label';
  lbl.textContent = label;
  const ind = document.createElement('span'); ind.className = 'ac-cfg-ind';
  lbl.appendChild(ind);
  wrap.appendChild(lbl);
  let el;
  if (multiline) { el = document.createElement('textarea'); el.rows = rows || 2; }
  else { el = document.createElement('input'); el.type = 'text'; }
  el.className = 'ac-input';
  if (field) el.dataset.field = field;
  el.value = value || '';
  if (placeholder) el.placeholder = placeholder;
  if (readonly) el.readOnly = true;
  wrap.appendChild(el);
  if (hint) { const h = document.createElement('div'); h.className = 'ac-hint'; h.textContent = hint; wrap.appendChild(h); }
  gbody.appendChild(wrap);
  if (onSave && !readonly) {
    const save = _debounced(async () => {
      _ovMarkSaving(ind);
      let ok = false;
      try { ok = await onSave(el.value); } catch (_) { ok = false; }
      _ovFlashCheck(ind, !!ok, ok ? '' : 'Save failed');
    });
    el.addEventListener('input', save);
    el.addEventListener('blur', () => save.flush());
  }
  return { wrap, el, ind };
}

/** A small themed popover anchored to a control. ``body`` is a plain string
 *  (split into paragraphs) or a DOM node (e.g. a <pre>). One at a time;
 *  dismiss on close, outside click, or Escape. Theming uses design-system
 *  tokens only (dark/light safe). */
function _acInfoPopover(anchorEl, title, body) {
  const old = document.getElementById('ac-info-popover');
  if (old) old.remove();

  const pop = document.createElement('div');
  pop.id = 'ac-info-popover';
  pop.style.cssText = [
    'position:fixed', 'z-index:9999',
    'max-width:360px', 'min-width:250px',
    'background:var(--bg-elev-2, var(--bg-1))',
    'border:var(--border-width) solid var(--border-strong)',
    'border-radius:10px',
    'box-shadow:var(--shadow-float)',
    'padding:12px 14px',
    'font-size:12.5px', 'line-height:1.55',
    'color:var(--fg-1)',
  ].join(';');

  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;';
  const h = document.createElement('div');
  h.style.cssText = 'font-weight:600;font-size:12.5px;color:var(--accent);';
  h.textContent = title;
  const close = document.createElement('button');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  close.title = 'Close';
  close.style.cssText = 'background:none;border:none;cursor:pointer;color:var(--fg-2);padding:2px;display:inline-flex;align-items:center;border-radius:4px;';
  close.innerHTML = icon('x', { size: '14px' });
  head.appendChild(h);
  head.appendChild(close);
  pop.appendChild(head);

  if (typeof body === 'string') {
    body.split(/\n+/).forEach(para => {
      const t = para.trim();
      if (!t) return;
      const p = document.createElement('div');
      p.style.cssText = 'color:var(--fg-2);margin-bottom:8px;white-space:pre-wrap;';
      p.textContent = t;
      pop.appendChild(p);
    });
  } else {
    pop.appendChild(body);
  }

  document.body.appendChild(pop);

  const r = anchorEl.getBoundingClientRect();
  let top = r.bottom + 8;
  let left = r.left;
  const pw = pop.offsetWidth || 300;
  const ph = pop.offsetHeight || 60;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  pop.style.top = top + 'px';
  pop.style.left = left + 'px';

  close.addEventListener('click', () => pop.remove());

  const cleanup = () => {
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const outside = (ev) => {
    const cur = document.getElementById('ac-info-popover');
    if (!cur) { cleanup(); return; }
    if (!cur.contains(ev.target) && ev.target !== anchorEl) { cur.remove(); cleanup(); }
  };
  const onKey = (ev) => {
    if (ev.key !== 'Escape') return;
    const cur = document.getElementById('ac-info-popover');
    if (cur) { cur.remove(); cleanup(); }
  };
  setTimeout(() => {
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}

/** Fetch the GLOBAL closer template (app-prompts.json → app_level_prompts.output_closer,
 *  legacy keys output_summarizer / output_overviewer) and show it in a popover. */
function _showGlobalCloserPrompt(anchorEl) {
  (async () => {
    let tpl = '';
    try {
      const res = await fetch(apiPath('/api/v1/app-prompts'));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const lp = (data && data.app_level_prompts) || {};
      const pick = (entry) => entry && (entry.template || entry.text);
      tpl = pick(lp.output_closer) || pick(lp.output_summarizer) || pick(lp.output_overviewer) || '';
      if (!tpl) tpl = '(No global closer template is configured.)';
    } catch (e) {
      tpl = 'Could not load the global closer prompt: ' + (e && e.message ? e.message : e);
    }
    const pre = document.createElement('pre');
    pre.style.cssText = 'white-space:pre-wrap;word-break:break-word;font-family:var(--font-mono,monospace);font-size:11.5px;line-height:1.5;color:var(--fg-2);max-height:280px;overflow:auto;margin:0;';
    pre.textContent = tpl;
    _acInfoPopover(anchorEl, 'Global closer prompt', pre);
  })();
}

/** Save a partial agent update with the on-top overlay over `ctrl` (the row's
 *  `.ac-config-control` slot). Silent so the open card isn't rebuilt on each edit;
 *  caches still update inside `_putAgentField`. Returns the ok boolean. */
async function _saveCfg(agent, updates, ctrl) {
  _ovMarkSaving(ctrl);
  const ok = await _putAgentField(agent, updates, null, { silent: true });
  _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
  return ok;
}

// Explicit card saves must not signal success just because the PUT returned 200.
// The API can normalize or reject individual fields, so compare the authoritative
// returned agent record before allowing the card-level green confirmation.
function _matchesPersisted(actual, expected) {
  if (expected === null || typeof expected !== 'object') return actual === expected;
  if (actual === null || typeof actual !== 'object') return false;
  if (Array.isArray(expected)) {
    return Array.isArray(actual) && actual.length === expected.length
      && expected.every((value, index) => _matchesPersisted(actual[index], value));
  }
  return Object.entries(expected).every(([key, value]) => _matchesPersisted(actual[key], value));
}

async function _saveCfgVerified(agent, updates, card, verify) {
  _ovMarkSaving(card);
  const persisted = await _putAgentField(agent, updates, null, { silent: true, returnAgent: true });
  const verified = !!persisted && verify(persisted);
  _ovFlashCheck(card, verified, verified ? '' : 'Server did not confirm the saved configuration');
  return verified ? persisted : null;
}

// A Modes card is deliberately save-on-demand. Its button is part of expanded
// content (never the summary), stays hidden until that card is dirty, and clears
// only after its caller has verified the server's returned agent record.
function _attachCardSaveButton(card, fields, isEditable, save) {
  if (!isEditable) return () => {};
  let dirty = false;
  let saving = false;
  const row = document.createElement('div');
  row.style.cssText = 'grid-column:1/-1;display:flex;justify-content:flex-end;margin-top:4px;';
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'ac-btn ac-btn-primary';
  button.dataset.modeCardSave = 'true';
  button.textContent = 'SAVE CHANGES';
  row.appendChild(button); fields.appendChild(row);
  const refresh = () => { row.hidden = !(dirty && card.open); button.disabled = saving; };
  const markDirty = () => { dirty = true; refresh(); };
  card.addEventListener('toggle', refresh);
  button.addEventListener('click', async () => {
    if (saving || !dirty) return;
    saving = true; refresh();
    const verified = await save();
    if (verified) dirty = false;
    saving = false; refresh();
  });
  refresh();
  return markDirty;
}

// Resting tooltip for the Config tab's danger-zone trash button. Reused by the
// fail-path reset (resetDeleteBtn) so the two references can't drift apart.
const TRASH_REST_TITLE = 'Move to the recycling bin';
// Resting accessible name (aria-label) for the same button — the more
// descriptive companion to TRASH_REST_TITLE. Set at creation in
// _renderConfigTab and passed to resetDeleteBtn on the fail path so the
// resting label and its reset can't drift apart either.
const TRASH_REST_LABEL = 'Move this agent to the recycling bin';

/** Soft-delete this agent (and, server-side, all of its sessions) to the
 *  recycling bin: DELETE the agent, then clear the open card, refresh the grid
 *  via the window callback registered in view.js, and show the bin notice.
 *  Mirrors the Claude card's in-panel delete flow (claude-agent.js). */
async function _trashAgent(btn, agent) {
  const name = agent.name || agent.id;
  // ── UX hardening: prevent double-submit while the delete is in flight ──
  // The two-click confirm already guards the trash button itself (advanceDeleteBtn
  // ignores clicks once BUSY), but the button stays visually enabled and any other
  // danger-zone button on the card could still fire. Disable the whole danger-zone
  // group for the duration of the request; re-enable it if the delete fails (on
  // success the card is torn down by _clearExpanded + __agentsReload anyway).
  const zone = btn && btn.closest('.ac-category-group');
  const setDangerEnabled = (enabled) => {
    if (btn) btn.disabled = !enabled;
    if (zone) zone.querySelectorAll('button').forEach(b => { b.disabled = !enabled; });
  };
  setDangerEnabled(false);
  // ── Failure notice: honest error feedback, card stays open for a retry ──
  // On any delete failure we re-enable the danger zone (above) and surface a
  // visible error toast in danger styling that says the agent could NOT be
  // moved. The card is deliberately left open (no _clearExpanded on this path)
  // so the user can re-arm the trash button and retry.
  const fail = (msg) => {
    if (btn) resetDeleteBtn(btn, { size: '14px', title: TRASH_REST_TITLE, label: TRASH_REST_LABEL });
    setDangerEnabled(true); // re-enable the danger zone so the user can retry
    if (typeof window.__agentsNotice === 'function') {
      window.__agentsNotice(`Agent "${name}" could NOT be moved to the recycling bin — ${msg}`, { kind: 'error' });
    }
  };
  try {
    const res = await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE', headers: { ...authHeaders() } });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      fail(err.detail || `the server returned an error (HTTP ${res.status})`);
      return;
    }
  } catch (e) {
    fail(`network error — ${e.message}`);
    return;
  }
  try { if (typeof app.populateAgentSelect === 'function') await app.populateAgentSelect(app.currentUserId); } catch (_) { /* non-fatal */ }
  _clearExpanded();
  if (typeof window.__agentsReload === 'function') window.__agentsReload();
  if (typeof window.__agentsNotice === 'function') {
    window.__agentsNotice(`Agent "${name}" moved to the recycling bin — its sessions went with it.`, {
      action: { label: 'Open bin', fn: () => { if (typeof window.__agentsOpenBin === 'function') window.__agentsOpenBin(); } },
    });
  }
}

// ── Main render ───────────────────────────────────────────────────────────────

function _renderManagerLoopConfig(body, agent) {
  const d = {
    enabled:false, model:null, effort:null,
    budgets:{max_checks:9,max_blocks:3,by_trigger:{plan_gate:1,edit_gate:4,watchdog:3,commit_gate:1}},
    starter:{enabled:false,parallel:true,wait_before_write:false,inherit_prior_summary:true,seed_plan:true,seed_checklist:true,prompt:''},
    communication:{working_intro:'brief',intro_timing:'before_preparation',intro_timeout_seconds:12,update_policy:'material',scout_visibility:'background'},
    contracts:{enabled:false,engine:'subagent',failure_policy:'hybrid',scout:{enabled:true,timeout_seconds:60,model:null},edit_review:{enabled:true,policy:'blocking',timeout_seconds:60,max_checks:4,model:null},close_review:{enabled:true,policy:'blocking',timeout_seconds:120,max_rounds:1,model:null}},
    triggers:{plan_gate:{policy:'off',modes:['plan','auto'],prompt:''},edit_gate:{policy:'off',modes:['auto'],prompt:''},commit_gate:{policy:'off',modes:['auto'],prompt:''}},
    watchdog:{enabled:false,every_n_turns:0,on_errors:0,error_window:8,on_stall:true,cooldown_turns:2,action:'advise',modes:['ask','plan','auto'],prompt:''},
    closer:{enabled:true,audit_mode_contract:true,audit_agent_checklist:true,require_plan_document:true,require_manager_clear:false,max_rounds:1,send_back:true}, mode_overrides:{},
  };
  const src = agent.manager_loop && typeof agent.manager_loop === 'object' ? agent.manager_loop : {};
  const cfg = {
    ...structuredClone(d), ...structuredClone(src),
    budgets:{...d.budgets,...(src.budgets||{}),by_trigger:{...d.budgets.by_trigger,...((src.budgets||{}).by_trigger||{})}},
    starter:{...d.starter,...(src.starter||{})},
    communication:{...d.communication,...(src.communication||{})},
    contracts:{...d.contracts,...(src.contracts||{}),scout:{...d.contracts.scout,...((src.contracts||{}).scout||{})},edit_review:{...d.contracts.edit_review,...((src.contracts||{}).edit_review||{})},close_review:{...d.contracts.close_review,...((src.contracts||{}).close_review||{})}},
    triggers:Object.fromEntries(Object.keys(d.triggers).map(k=>[k,{...d.triggers[k],...((src.triggers||{})[k]||{})}])),
    watchdog:{...d.watchdog,...(src.watchdog||{})}, closer:{...d.closer,...(src.closer||{})}, mode_overrides:{...(src.mode_overrides||{})},
  };
  const g = _group(body, 'workflow', 'Manager loop',
    'A bounded supervisor around the main run. It can use one-shot checks or contract-bound orchestration workers for Scout, edit review, and independent close audits.');
  g.classList.add('ac-manager-loop'); const list = _cfgList(g);
  const save = _debounced(()=>{agent.manager_loop=structuredClone(cfg);_saveCfg(agent,{manager_loop:cfg},g);});
  const check=(host,label,value,set)=>{const w=document.createElement('label');w.className='ac-manager-check';const e=document.createElement('input');e.type='checkbox';e.checked=!!value;const s=document.createElement('span');s.textContent=label;w.append(e,s);host.appendChild(w);e.addEventListener('change',()=>{set(e.checked);save();});return e;};
  const number=(host,label,value,min,max,set)=>{const w=document.createElement('label');w.className='ac-manager-field ac-manager-field-number';const s=document.createElement('span');s.textContent=label;const e=document.createElement('input');e.type='number';e.className='ac-input';e.min=min;e.max=max;e.value=value;e.addEventListener('input',()=>{set(Math.max(min,Math.min(max,parseInt(e.value,10)||0)));save();});w.append(s,e);host.appendChild(w);};
  const text=(host,label,value,set,placeholder='')=>{const w=document.createElement('label');w.className='ac-manager-field';const s=document.createElement('span');s.textContent=label;const e=document.createElement('textarea');e.className='ac-input';e.rows=3;e.value=value||'';e.placeholder=placeholder;e.addEventListener('input',()=>{set(e.value);save();});w.append(s,e);host.appendChild(w);};
  const select=(host,label,value,opts,set)=>{const w=document.createElement('label');w.className='ac-manager-field ac-manager-field-number';const s=document.createElement('span');s.textContent=label;const e=document.createElement('select');e.className='ac-input';opts.forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;o.selected=v===value;e.appendChild(o);});e.addEventListener('change',()=>{set(e.value);save();});w.append(s,e);host.appendChild(w);};
  const stage=(name,hint)=>{const el=document.createElement('details');el.className='ac-manager-stage';const h=document.createElement('summary');const n=document.createElement('span');n.textContent=name;const i=document.createElement('span');i.textContent=hint;h.append(n,i);el.appendChild(h);const host=document.createElement('div');host.className='ac-manager-stage-body';el.appendChild(host);g.appendChild(el);return host;};
  const modes=(host,target)=>{const box=document.createElement('div');box.className='ac-manager-modes';const cap=document.createElement('span');cap.textContent='Applies in';box.appendChild(cap);const all=Array.isArray(agent.execution_modes)&&agent.execution_modes.length?agent.execution_modes:[{id:'ask',label:'Ask'},{id:'plan',label:'Plan'},{id:'auto',label:'Auto'}];all.forEach(m=>check(box,m.label||m.id,(target.modes||[]).includes(m.id),on=>{const set=new Set(target.modes||[]);on?set.add(m.id):set.delete(m.id);target.modes=[...set];}));host.appendChild(box);};

  const enabledRow=_cfgRow(list,'Enable Manager loop','Use quick one-shot second opinions around this agent\'s main run.');check(enabledRow.ctrl,'',cfg.enabled,v=>{cfg.enabled=v;gateCloser();});
  const presetRow=_cfgRow(list,'Managed build preset','The primary agent introduces its work first; Scout runs silently, valid edit objections block, and two independent close reviews are required. Infrastructure failures remain visible and fail open.');const preset=document.createElement('button');preset.type='button';preset.className='ac-btn ac-btn-secondary';preset.textContent='Apply managed build';preset.addEventListener('click',async()=>{if(preset.disabled)return;cfg.enabled=true;cfg.starter.enabled=true;cfg.starter.parallel=true;cfg.communication.working_intro='brief';cfg.communication.intro_timing='before_preparation';cfg.communication.update_policy='material';cfg.communication.scout_visibility='background';cfg.contracts.enabled=true;cfg.contracts.engine='subagent';cfg.contracts.failure_policy='hybrid';cfg.contracts.scout.enabled=true;cfg.contracts.edit_review.enabled=true;cfg.contracts.edit_review.policy='blocking';cfg.contracts.close_review.enabled=true;cfg.contracts.close_review.policy='blocking';cfg.contracts.close_review.max_rounds=1;agent.manager_loop=structuredClone(cfg);preset.disabled=true;preset.textContent='Applying…';const ok=await _saveCfg(agent,{manager_loop:cfg},g);const confirmed=ok&&!!(agent.manager_loop&&agent.manager_loop.contracts&&agent.manager_loop.contracts.enabled);preset.disabled=false;preset.textContent=confirmed?'Managed build applied':'Apply managed build';if(confirmed&&typeof window.__agentsNotice==='function')window.__agentsNotice('Managed build is enabled on the live agent.');});presetRow.ctrl.appendChild(preset);
  const preflightRow=_cfgRow(list,'Managed build preflight','All three runtime prerequisites must be available.');const preflight=document.createElement('div');preflight.className='ac-manager-preflight';preflight.textContent='Checking prerequisites…';preflightRow.ctrl.appendChild(preflight);(async()=>{let current=agent;try{const res=await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,{headers:authHeaders(),cache:'no-store'});if(res.ok){const data=await res.json();current=data.agent||agent;Object.assign(agent,current);}}catch(_){/* rendered below */}const p=current.manager_contract_preflight||{};const items=[['run_manager app function',p.run_manager],['manager_chk loop node',p.manager_chk],['Agent Orchestration ability',p.agent_orchestration]];preflight.replaceChildren();items.forEach(([label,ready])=>{const line=document.createElement('div');line.textContent=`${ready?'✓':'!'} ${label}`;line.className=ready?'ac-preflight-ok':'ac-preflight-missing';preflight.appendChild(line);});if(!p.ready){const link=document.createElement('a');link.href='/#instances/settings';link.textContent='Open admin settings';link.className='ac-link';preflight.appendChild(link);}if(agent.id==='shared_default'){const divergence=document.createElement('div');divergence.className='ac-manager-seed-state';divergence.textContent=current.shared_default_seed_diverged===false?'Live configuration matches the JSON seed.':current.shared_default_seed_diverged===true?'Live configuration differs from the JSON seed. Use “Push to JSON file” to update the seed.':'Seed comparison unavailable.';preflight.appendChild(divergence);}})();
  const modelRow=_cfgRow(list,'Manager model','Optional low-cost model override; blank uses the agent model.');const model=document.createElement('input');model.className='ac-input ac-input-sm';model.value=cfg.model||'';model.placeholder='Use agent model';model.addEventListener('input',()=>{cfg.model=model.value.trim()||null;save();});modelRow.ctrl.appendChild(model);
  const effortRow=_cfgRow(list,'Reasoning effort','Keep the second opinion small and fast.');const effort=document.createElement('select');effort.className='ac-input ac-input-sm ac-config-sel';[['','Default'],['low','Low'],['medium','Medium'],['high','High']].forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;o.selected=(cfg.effort||'')===v;effort.appendChild(o);});effort.addEventListener('change',()=>{cfg.effort=effort.value||null;save();});effortRow.ctrl.appendChild(effort);
  let h=stage('Budgets','Hard bounds keep the parallel judgments cheap');number(h,'Total checks',cfg.budgets.max_checks,0,50,v=>{cfg.budgets.max_checks=v;});number(h,'Blocking verdicts',cfg.budgets.max_blocks,0,20,v=>{cfg.budgets.max_blocks=v;});
  h=stage('Working agent communication','The primary model speaks before preparation; later updates stay concise');select(h,'Working introduction',cfg.communication.working_intro,[['off','Off'],['brief','Brief intent'],['plan','Short plan']],v=>{cfg.communication.working_intro=v;});number(h,'Introduction timeout (seconds)',cfg.communication.intro_timeout_seconds,1,60,v=>{cfg.communication.intro_timeout_seconds=v;});select(h,'Progress updates',cfg.communication.update_policy,[['off','Off'],['material','Material changes'],['phase','Major phases']],v=>{cfg.communication.update_policy=v;});select(h,'Scout visibility',cfg.communication.scout_visibility,[['background','Background'],['findings','Findings only'],['legacy_preview','Legacy provisional preview']],v=>{cfg.communication.scout_visibility=v;});
  h=stage('Starter / Run Scout','Silent parallel intake, prior-run linking, and plan seeding');check(h,'Enable Starter',cfg.starter.enabled,v=>{cfg.starter.enabled=v;});check(h,'Run in parallel',cfg.starter.parallel,v=>{cfg.starter.parallel=v;});check(h,'Wait before first write',cfg.starter.wait_before_write,v=>{cfg.starter.wait_before_write=v;});check(h,'Use prior-run handoff summary',cfg.starter.inherit_prior_summary,v=>{cfg.starter.inherit_prior_summary=v;});check(h,'Seed persistent plan',cfg.starter.seed_plan,v=>{cfg.starter.seed_plan=v;});check(h,'Seed accumulated checklist',cfg.starter.seed_checklist,v=>{cfg.starter.seed_checklist=v;});text(h,'Starter prompt',cfg.starter.prompt,v=>{cfg.starter.prompt=v;},'Default Starter instructions are shown and saved here.');
  h=stage('Subagent contracts','Persistent edit reviewer plus fresh, independent close auditors');check(h,'Enable contract workers',cfg.contracts.enabled,v=>{cfg.contracts.enabled=v;});select(h,'Infrastructure failure',cfg.contracts.failure_policy,[['hybrid','Hybrid — fail open visibly'],['fail_closed','Fail closed'],['advisory','Advisory']],v=>{cfg.contracts.failure_policy=v;});check(h,'Tool-free Scout worker',cfg.contracts.scout.enabled,v=>{cfg.contracts.scout.enabled=v;});number(h,'Scout timeout (seconds)',cfg.contracts.scout.timeout_seconds,1,600,v=>{cfg.contracts.scout.timeout_seconds=v;});check(h,'Persistent read-only edit reviewer',cfg.contracts.edit_review.enabled,v=>{cfg.contracts.edit_review.enabled=v;});select(h,'Edit policy',cfg.contracts.edit_review.policy,[['blocking','Blocking'],['async','Advisory'],['off','Off']],v=>{cfg.contracts.edit_review.policy=v;});number(h,'Edit checks per turn',cfg.contracts.edit_review.max_checks,1,100,v=>{cfg.contracts.edit_review.max_checks=v;});number(h,'Edit timeout (seconds)',cfg.contracts.edit_review.timeout_seconds,1,600,v=>{cfg.contracts.edit_review.timeout_seconds=v;});check(h,'Fresh alignment and evidence auditors',cfg.contracts.close_review.enabled,v=>{cfg.contracts.close_review.enabled=v;});select(h,'Close policy',cfg.contracts.close_review.policy,[['blocking','Blocking'],['async','Advisory'],['off','Off']],v=>{cfg.contracts.close_review.policy=v;});number(h,'Close correction rounds',cfg.contracts.close_review.max_rounds,0,5,v=>{cfg.contracts.close_review.max_rounds=v;});number(h,'Close timeout (seconds)',cfg.contracts.close_review.timeout_seconds,1,600,v=>{cfg.contracts.close_review.timeout_seconds=v;});
  const labels={plan_gate:'Plan gate',edit_gate:'Edit gate',commit_gate:'Commit gate'};
  Object.keys(labels).forEach(k=>{const t=cfg.triggers[k];const host=stage(labels[k],'Policy, applicable modes, budget, and prompt');select(host,'Behavior',t.policy,[['off','Off'],['async','Advisory / parallel'],['blocking','Blocking gate']],v=>{t.policy=v;});modes(host,t);number(host,'Maximum checks',cfg.budgets.by_trigger[k],0,20,v=>{cfg.budgets.by_trigger[k]=v;});text(host,'Manager prompt',t.prompt,v=>{t.prompt=v;},'The effective default prompt is shown and saved here.');});
  h=stage('Watchdog','Periodic, error-cluster, and stalled-progress reactions');check(h,'Enable Manager watchdog',cfg.watchdog.enabled,v=>{cfg.watchdog.enabled=v;});check(h,'React to stalls and loops',cfg.watchdog.on_stall,v=>{cfg.watchdog.on_stall=v;});number(h,'Every N turns (0 = off)',cfg.watchdog.every_n_turns,0,100,v=>{cfg.watchdog.every_n_turns=v;});number(h,'Error threshold (0 = off)',cfg.watchdog.on_errors,0,20,v=>{cfg.watchdog.on_errors=v;});number(h,'Error window',cfg.watchdog.error_window,1,100,v=>{cfg.watchdog.error_window=v;});number(h,'Cooldown turns',cfg.watchdog.cooldown_turns,0,100,v=>{cfg.watchdog.cooldown_turns=v;});number(h,'Maximum checks',cfg.budgets.by_trigger.watchdog,0,20,v=>{cfg.budgets.by_trigger.watchdog=v;});select(h,'Reaction',cfg.watchdog.action,[['advise','Advise'],['replan','Require replan'],['verify','Require verification'],['pause','Pause and ask'],['terminate','Terminate']],v=>{cfg.watchdog.action=v;});modes(h,cfg.watchdog);text(h,'Watchdog prompt',cfg.watchdog.prompt,v=>{cfg.watchdog.prompt=v;},'The effective default prompt is shown and saved here.');
  h=stage('Closer integration','Audit, final handoff guidance, and bounded send-back. Output Closer activation is controlled only by the Closing Message gate.');
  check(h,'Audit accumulated mode contracts',cfg.closer.audit_mode_contract,v=>{cfg.closer.audit_mode_contract=v;});check(h,'Audit the agent checklist below',cfg.closer.audit_agent_checklist,v=>{cfg.closer.audit_agent_checklist=v;});check(h,'Require persistent Plan document',cfg.closer.require_plan_document,v=>{cfg.closer.require_plan_document=v;});check(h,'Require blocking Manager feedback cleared',cfg.closer.require_manager_clear,v=>{cfg.closer.require_manager_clear=v;});check(h,'Send incomplete work back under the same mode',cfg.closer.send_back,v=>{cfg.closer.send_back=v;});number(h,'Maximum fix rounds',cfg.closer.max_rounds,0,5,v=>{cfg.closer.max_rounds=v;});text(h,'Closer output prompt',cfg.closer.prompt,v=>{cfg.closer.prompt=v;},'Concrete close-out instructions are saved here.');
  // The Manager switch controls these audit settings, not Output Closer activation.
  function gateCloser(){const active=!!cfg.enabled;h.querySelectorAll('input,select,textarea').forEach(el=>{el.disabled=!active;});}
  gateCloser();
}

export function _renderConfigTab(body, agent, panelEl, _renderList, { onPageChange = null } = {}) {
  const isEditable = (
    agent.source === 'custom' && agent.id !== 'shared_default'
  ) || (agent.id === 'shared_default' && _userIsAdmin);
  const isMock = _isMockAgent(agent);
  const isAnonymousDraft = isMock && isAnonGuest();

  // Broad parent topics use the App Settings list → one page lifecycle. The
  // content nodes below are mounted once, then the controller moves the selected
  // node into its dedicated page host instead of exposing an inline accordion.
  const pageController = createAgentConfigPageController(body, {
    onPageChange: (pageId) => {
      _setAgentConfigPage(agent.id, pageId);
      onPageChange?.(pageId);
    },
  });
  const landing = pageController.landing;
  const recentTopics = _createRecentConfigTopicsGroup(landing);
  const group = document.createElement('section');
  group.className = 'agent-config-settings-group';
  group.setAttribute('aria-label', 'Agent configuration');
  const groupLabel = document.createElement('div');
  groupLabel.className = 'agent-config-settings-group-label';
  groupLabel.innerHTML = `${icon('settings-2', { size: '14px' })}<span>Agent configuration</span>`;
  const topicHost = document.createElement('div');
  topicHost.className = 'agent-config-topics';
  group.append(groupLabel, topicHost);
  landing.appendChild(group);

  const topicById = new Map();
  let recentIds = _readRecentConfigTopicIds(agent);
  let advancedVisible = false;
  const renderRecentTopics = () => recentTopics.render(
    recentIds.filter(id => advancedVisible || !ADVANCED_CONFIG_TOPIC_IDS.has(id)),
    topicById,
  );
  const rememberRecentTopic = (topicId) => {
    recentIds = [topicId, ...recentIds.filter(id => id !== topicId)].slice(0, RECENT_CONFIG_TOPICS_LIMIT);
    _writeRecentConfigTopicIds(agent, recentIds);
    renderRecentTopics();
    if (isMock || !app.currentUserId) return;
    fetch(_recentConfigTopicsUrl(agent), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ topic_id: topicId }),
    }).then(async response => {
      if (!response.ok) return;
      const payload = await response.json();
      if (!Array.isArray(payload.topic_ids)) return;
      recentIds = payload.topic_ids.filter(id => RECENT_CONFIG_TOPIC_IDS.has(id)).slice(0, RECENT_CONFIG_TOPICS_LIMIT);
      _writeRecentConfigTopicIds(agent, recentIds);
      renderRecentTopics();
    }).catch(() => { /* server recency is best-effort while the local cache remains available */ });
  };
  const addTopic = (page, options = {}) => {
    const { id, iconName, title, description } = page;
    const content = _topic(topicHost, agent, iconName, title, {
      ...options,
      description,
      recentId: id,
      onOpen: rememberRecentTopic,
      onNavigate: () => pageController.open(id),
    });
    pageController.register(id, content);
    topicById.set(id, {
      iconName, title, description, content,
      open: () => content._openAgentConfigTopic?.(),
    });
    return content;
  };
  const topicOptions = new Map([['identity', { open: false }]]);
  const configTopics = new Map(AGENT_CONFIG_PAGES.map((page) => [
    page.id,
    addTopic(page, topicOptions.get(page.id)),
  ]));
  const identityTopic = configTopics.get('identity');
  const modelTopic = configTopics.get('model');
  const voiceTopic = configTopics.get('voice');
  const deployTopic = configTopics.get('deploy');
  const modesTopic = configTopics.get('modes');
  const managerTopic = configTopics.get('manager');
  const runtimeTopic = configTopics.get('runtime');
  renderRecentTopics();
  if (!isMock && app.currentUserId) {
    fetch(_recentConfigTopicsUrl(agent), { headers: authHeaders() }).then(async response => {
      if (!response.ok) return;
      const payload = await response.json();
      if (!Array.isArray(payload.topic_ids)) return;
      recentIds = payload.topic_ids.filter(id => RECENT_CONFIG_TOPIC_IDS.has(id)).slice(0, RECENT_CONFIG_TOPICS_LIMIT);
      _writeRecentConfigTopicIds(agent, recentIds);
      renderRecentTopics();
    }).catch(() => { /* cached recents remain visible while offline */ });
  }
  // `_topic()` returns its canonical content element. Hide each parent row so
  // Advanced topics cannot be opened from the landing until explicitly enabled.
  const advancedTopics = AGENT_CONFIG_PAGES
    .filter((page) => page.advanced)
    .map((page) => configTopics.get(page.id)?.parentElement)
    .filter(Boolean);
  const setAdvancedVisibility = (show) => {
    advancedVisible = !!show;
    advancedTopics.forEach(topic => { topic.hidden = !advancedVisible; });
    renderRecentTopics();
  };
  // Advanced sections begin hidden each time the Config tab is rendered.
  setAdvancedVisibility(false);

  // New-agent drafts and persisted agents mount the same Identity renderer.
  // Keep the canonical renderer call identical for both paths; the topic body
  // identifies itself and lets the shared renderer omit its duplicate heading.
  {
    const body = identityTopic;
    renderAgentIdentitySettings(body, agent);
  }

  // ── Danger zone: delete this agent (soft-delete to the recycling bin) ──────
  // Only persisted custom agents can be trashed — system agents (template rows)
  // are protected server-side, and the mock/new-agent draft isn't persisted.
  // Hidden in the bin view (a trashed agent's card still opens when expanded).
  if (isEditable && agent.id !== 'shared_default' && !isMock && !_binView) {
    // Horizontal rule separating the Chat design section from the danger zone.
    // A sibling of both anchors (NOT a descendant of either), inserted between
    // the embedded identity settings and the danger zone group below.
    const divider = document.createElement('hr');
    divider.className = 'agent-config-divider';
    identityTopic.appendChild(divider);
    const g = _group(identityTopic, 'trash-2', 'Danger zone');
    // _group returns the .ac-category-body; the .ac-category-summary (with its
    // icon) is a SIBLING of it, so reach the icon via the parent group element.
    const gIcon = g.parentElement && g.parentElement.querySelector('.ac-category-summary i');
    if (gIcon) gIcon.style.color = 'var(--danger)';
    // Info tooltip after the "Danger zone" heading — carries the guidance that
    // used to be the Delete-agent row description (now collapsed into a hover/
    // focus bubble, matching the Chat design section).
    const gSummary = g.parentElement && g.parentElement.querySelector('.ac-category-summary');
    if (gSummary) {
      const tip = document.createElement('span');
      tip.className = 'agent-config-info-tip';
      tip.setAttribute('data-tooltip', 'Move this agent and all of its sessions to the recycling bin. Nothing is erased — you can restore them from the bin.');
      tip.tabIndex = 0;
      tip.setAttribute('aria-label', 'About the danger zone');
      tip.innerHTML = icon('info', { size: '14px' });
      const titleEl = gSummary.querySelector('.ac-category-title');
      if (titleEl) titleEl.after(tip);
    }
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Delete agent');
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'agents-config-delete-btn';
    delBtn.title = TRASH_REST_TITLE;
    delBtn.setAttribute('aria-label', TRASH_REST_LABEL);
    delBtn.innerHTML = icon('trash-2', { size: '14px' });
    delBtn.addEventListener('click', () => {
      advanceDeleteBtn(delBtn, {
        size: '14px', spinSize: '14px',
        armTitle: 'Click again to move to the recycling bin',
        armLabel: 'Click again to move this agent to the recycling bin',
        busyTitle: 'Moving to the recycling bin\u2026',
        busyLabel: 'Moving this agent to the recycling bin\u2026',
        onConfirm: () => _trashAgent(delBtn, agent),
      });
    });
    ctrl.appendChild(delBtn);
  }

  if (isEditable) {
    renderVoiceConversationSettings(voiceTopic, agent);
  }

  // Manager Loop is a bounded collection of cheap, one-shot second opinions.
  // It shares one normalized contract with Starter, trigger gates, Watchdog,
  // modes, and Closer; raw tool output is deliberately not included.
  if (isEditable) {
    _renderManagerLoopConfig(managerTopic, agent);
  }

  // Stored inside the existing per-agent chat_ui override so the chat shell
  // resolves these through the same global + agent profile merge as its other
  // chrome. An empty array is intentional: this agent shows no starters.
  if (isEditable) {
    let starters = agent.chat_ui?.chat_common?.suggestion_prompts;
    if (!Array.isArray(starters) && (agent.id === 'shared_default' || agent.id === 'default')) {
      starters = DEFAULT_WEBAGENT_SUGGESTION_PROMPTS;
    }
    const g = _group(modesTopic, 'sparkles', 'Starter prompts',
      'Optional prompts shown above the composer when this agent opens a brand-new, empty session. Put one prompt on each line. Leave the field empty to show none.');
    _cfgField(g, {
      label: 'Suggested first messages',
      field: 'suggestion_prompts',
      value: Array.isArray(starters) ? starters.join('\n') : '',
      multiline: true,
      rows: 4,
      placeholder: 'Summarize what I should focus on today\nHelp me plan a new project',
      hint: 'One prompt per line. These appear only before the first real message.',
      onSave: async (value) => {
        const suggestionPrompts = value.split('\n').map(line => line.trim()).filter(Boolean);
        const ok = await _putAgentField(agent, {
          chat_ui: { chat_common: { suggestion_prompts: suggestionPrompts } },
        }, null, { silent: true });
        if (ok && app.currentAgentId === agent.id && typeof app.refreshSuggestions === 'function') {
          try { app.refreshSuggestions(); } catch (_) { /* chat may not be mounted */ }
        }
        return ok;
      },
    });
  }

  // ── Closer checklist (per-agent close-out contract) ───────────────────────
  if (isEditable) {
    // ── Checklist (contract questions) ──
    const g2 = _group(managerTopic, 'clipboard-check', 'Closer checklist',
      'The checklist is the quality bar the closer holds the agent\'s finished work to.\n\n'
      + 'After a run completes, the closer reads the conversation (your messages and the agent\'s responses — not the raw tool logs) and checks each item one by one. Every item must show evidence it was actually done.\n\n'
      + 'All items pass → the closer writes a clean summary. Any item missing → the closer sends the agent back with feedback to finish it. "Max fix rounds" caps how many times that can happen (0 = never send back). When the rounds run out, the summary honestly flags what is still missing.\n\n'
      + 'Tip: write items that can be judged from what the agent says it did, e.g. "The change was verified — tests run or the edited file re-read."');

    const ac = agent.audit_checklist;
    const managerCloser = agent.manager_loop && agent.manager_loop.closer && typeof agent.manager_loop.closer === 'object'
      ? agent.manager_loop.closer : null;
    const startItems = [];
    let roundsVal = 1;
    let sbVal = true;
    if (typeof ac === 'string') startItems.push(...ac.split('\n').map(s => s.trim()).filter(Boolean));
    else if (Array.isArray(ac)) startItems.push(...ac.map(s => String(s).trim()).filter(Boolean));
    else if (ac && typeof ac === 'object') {
      const cl = ac.checklist;
      if (Array.isArray(cl)) startItems.push(...cl.map(s => String(s).trim()).filter(Boolean));
      else if (typeof cl === 'string') startItems.push(...cl.split('\n').map(s => s.trim()).filter(Boolean));
      if (typeof ac.max_rounds === 'number') roundsVal = ac.max_rounds;
      if (typeof ac.send_back === 'boolean') sbVal = ac.send_back;
    }
    if (managerCloser && typeof managerCloser.max_rounds === 'number') roundsVal = managerCloser.max_rounds;
    if (managerCloser && typeof managerCloser.send_back === 'boolean') sbVal = managerCloser.send_back;

    const chkWrap = document.createElement('div');
    chkWrap.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
    g2.appendChild(chkWrap);

    const collectItems = () => Array.from(chkWrap.querySelectorAll('input.ac-closer-item-input'))
      .map(i => i.value.trim()).filter(Boolean);

    const saveChecklist = () => {
      const vals = collectItems();
      const maxRounds = parseInt(roundsEl.value, 10) || 0;
      const nextManager = structuredClone(agent.manager_loop || {});
      nextManager.closer = { ...(nextManager.closer || {}), max_rounds: maxRounds, send_back: cb.checked };
      agent.manager_loop = nextManager;
      _putAgentField(agent, {
        audit_checklist: vals.length ? { checklist: vals, max_rounds: maxRounds, send_back: cb.checked } : '',
        manager_loop: nextManager,
      }, null, { silent: true });
    };
    const saveChecklistDeb = _debounced(saveChecklist);

    // The one blank "add" row that always lives at the bottom.
    const blankRow = document.createElement('div');
    blankRow.style.cssText = 'display:flex;gap:6px;align-items:center;';
    const blankInp = document.createElement('input');
    blankInp.type = 'text';
    blankInp.className = 'ac-input ac-closer-item-input';
    blankInp.placeholder = 'Add a checklist item';
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.setAttribute('aria-label', 'Add checklist item');
    addBtn.title = 'Add item';
    addBtn.style.cssText = 'flex:none;display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:6px;background:none;border:var(--border-width) solid var(--border);color:var(--fg-2);cursor:pointer;';
    addBtn.innerHTML = icon('plus', { size: '14px' });
    blankRow.appendChild(blankInp);
    blankRow.appendChild(addBtn);
    chkWrap.appendChild(blankRow);

    // One row per existing item: input + minus (delete) button.
    const addItemRow = (value) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:6px;align-items:center;';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'ac-input ac-closer-item-input';
      inp.value = value;
      inp.addEventListener('input', saveChecklistDeb);
      const del = document.createElement('button');
      del.type = 'button';
      del.setAttribute('aria-label', 'Delete checklist item');
      del.title = 'Delete item';
      del.style.cssText = 'flex:none;display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:6px;background:none;border:var(--border-width) solid var(--border);color:var(--fg-2);cursor:pointer;';
      del.innerHTML = icon('minus', { size: '14px' });
      del.addEventListener('click', () => { row.remove(); saveChecklist(); });
      row.appendChild(inp);
      row.appendChild(del);
      chkWrap.insertBefore(row, blankRow);
      return row;
    };
    startItems.forEach(v => addItemRow(v));

    const commitBlank = () => {
      const v = blankInp.value.trim();
      if (!v) return;
      addItemRow(v);
      blankInp.value = '';
      blankInp.focus();
      saveChecklist();
    };
    addBtn.addEventListener('click', commitBlank);
    blankInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); commitBlank(); } });

    // Max fix rounds + send-back on failure.
    const list2 = _cfgList(g2);
    const roundsRow = _cfgRow(list2, 'Max fix rounds',
      'How many times a failed audit may send the agent back before the summary flags the missing items. 0 = never send back.');
    const roundsEl = document.createElement('input'); roundsEl.type = 'number';
    roundsEl.className = 'ac-input ac-input-sm ac-config-num';
    roundsEl.min = 0; roundsEl.max = 5; roundsEl.step = 1; roundsEl.value = roundsVal;
    roundsRow.ctrl.appendChild(_wrapNumberStepper(roundsEl));
    roundsEl.addEventListener('input', saveChecklist);

    const sbRow = _cfgRow(list2, 'Send back on failure',
      'When a checklist item is missing, re-run the agent with the feedback so it can fix it.');
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = sbVal;
    sbRow.ctrl.appendChild(cb);
    cb.addEventListener('change', saveChecklist);
  }

  // Suggested replies config for user-impersonator
  if (agent.id === 'user-impersonator') {
    _renderSuggestionModeControl(modesTopic);
  }

  // Local Claude Code agents never reach this tab — they get their own distinct,
  // tab-less card (renderClaudeSettings in ui/main-panel/agents/js/claude-agent.js,
  // diverted from _renderAgentCard in ui/main-panel/agents/js/view.js).

  // ── Template (mock / new-agent only) ──────────────────────────────────────
  if (isMock) {
    const g = _group(identityTopic, 'layout-template', 'Template');
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Template', 'Start this agent from a saved template.');
    const tplSelect = document.createElement('select');
    tplSelect.className = 'ac-input ac-config-sel';
    tplSelect.dataset.field = 'template';      // read by view.js _acceptWebAgentCreate
    tplSelect.innerHTML = '<option value="">— No template —</option>';
    ctrl.appendChild(tplSelect);

    (async () => {
      try {
        const url = `/api/v1/agents/templates?user_id=${encodeURIComponent(app.currentUserId)}&discoverable_only=true`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          // The Claude / Terminal engines have their own segments in the create
          // card's type chooser — keep their templates out of the WebAgent dropdown.
          (data.templates || []).filter(t => t.engine !== 'claude_code' && t.engine !== 'terminal_chat').forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name || t.id;
            if (t.id === 'default') opt.selected = true;
            tplSelect.appendChild(opt);
          });
        }
      } catch (e) { console.warn('agents: failed to load templates for mock', e); }
    })();
  }

  // ── Model (per-agent LLM — shared model-table component) ──────────────────
  // SISTER-PANEL: MODEL-TABLE — the SAME component as Admin → Agent Settings →
  // Models (ui/shared/js/model-table.js); keep the two mirrored. The table lists
  // ONLY this agent's own roster (editable, stored in
  // agent.llm_config.multi_providers). There are no read-only "Inherited" rows:
  // the app no longer lends an app-level default model to an agent, so the app
  // catalog is an ADOPTION SOURCE (the Model page's add picker), never something
  // the agent silently runs on. With no roster row usable for text the agent has
  // no Standard model and a chat cannot start on it. The chat user can still
  // switch the model per conversation (resolved server-side in
  // app/admin/settings.py).
  if (isEditable && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
    const llmCfg = agent.llm_config || { multi_providers: [] };

    const g = document.createElement('div');
    modelTopic.appendChild(g);
    // MODEL-ANALYTICS BOUNDARY: this remains a standalone sibling directly
    // before the shared selection table, never a table row or table descendant.
    const analyticsHost = document.createElement('div');
    analyticsHost.className = 'agent-model-analytics-host';
    g.appendChild(analyticsHost);
    const host = document.createElement('div');
    host.style.marginTop = '8px';
    g.appendChild(host);

    // Local mirror of the agent's llm_config blob; persisted whole via the agent
    // PUT (the backend reads metadata['llm_config'] = {use_default, provider,
    // base_url, api_key, model, multi_providers}).
    const agentCfg = { ...llmCfg };
    function persistLlm() {
      // silent: the model table renders every change in place, so a card rebuild
      // here would tear the panel down and re-mount it under the user (losing
      // scroll / expanded rows). Caches still update inside _putAgentField.
      return _putAgentField(agent, { llm_config: { ...agentCfg } }, null, { silent: true })
        .then(ok => { if (!ok) throw new Error('Save failed'); });
    }
    // The agent roster is an explicit adoption boundary. Available models and
    // App Catalog entries are picker sources only; they are never injected into
    // this table as inherited/read-only rows.
    // THIS user's per-model usage with THIS agent (cost + tokens) — scoped to the
    // (viewing user, agent) pair, NOT the agent's all-users total. Fetched once and
    // shared by every model row's USAGE column. Each call was priced at the model it
    // ran on (usage_events.cost_usd), so this is accurate across model switches.
    // See GET /admin/settings/agent-usage.
    let _agentUsagePromise = null;
    // Single-agent model selection uses the caller's entitled WebAgent Catalog as
    // a picker source. It is not inherited into the agent: selecting a row saves
    // only its opaque entry_id, and the agent API rehydrates the entitled runtime
    // row server-side while preserving this agent's role assignments.
    let platformCatalogModels = [];
    async function _loadPlatformCatalog() {
      const res = await fetch('/api/v1/entitlements/me/model-catalog', { headers: { ...authHeaders() } });
      if (!res.ok) throw new Error(`Unable to load WebAgent Catalog (HTTP ${res.status})`);
      const payload = await res.json();
      platformCatalogModels = Array.isArray(payload?.available_platform_models)
        ? payload.available_platform_models
        : [];
      return payload || {};
    }
    function _fetchAgentUsage() {
      if (!_agentUsagePromise) {
        const headers = { ...authHeaders() };
        _agentUsagePromise = fetch(`/admin/settings/agent-usage?agent_id=${encodeURIComponent(agent.id || '')}`, { headers })
          .then(r => r.ok ? r.json() : null).catch(() => null);
      }
      return _agentUsagePromise;
    }
    const adapter = {
      // Feed the model table's USAGE column. Both inherited (app-default) rows and
      // the agent's own pinned rows report the SAME scope: what THIS user has spent
      // running THIS agent on that model — pulled from the single per-(user, agent)
      // agent-usage breakdown. So every USAGE cell is the viewer's own spend with
      // this agent, never the agent's all-users total, the user's all-agents total,
      // or the app-wide total. (inherited is accepted but no longer changes scope.)
      fetchModelUsage: async ({ model, entry_id: entryId }) => {
        const d = await _fetchAgentUsage();
        // MODEL-ROW-USAGE: prefer the exact saved row's own ledger. Two rows can
        // share a model/provider with different credentials/effort/injections, so
        // the shared by_model bucket must only back the LEGACY (pre-entry_id)
        // events — and only while this is the model's sole roster row, otherwise
        // both rows would show the same combined total (the bug this fixes).
        // A backend that does not yet return by_entry (rolling deploy) keeps the
        // legacy model bucket so the table never blanks out mid-upgrade.
        const hasEntryBuckets = !!(d && d.by_entry);
        let u = entryId && d && d.by_entry && d.by_entry[entryId];
        if (!u && (!hasEntryBuckets || !entryId)) {
          u = d && d.by_model && d.by_model[model];
        }
        if (!u && entryId && hasEntryBuckets) {
          const rows = Array.isArray(agentCfg.multi_providers) ? agentCfg.multi_providers : [];
          const sameModel = rows.filter(row => String(row.model || '') === String(model || ''));
          if (sameModel.length <= 1) u = d && d.by_model && d.by_model[model];
        }
        if (!u) return { total_input_tokens: 0, total_output_tokens: 0, total_cost_usd: 0 };
        return {
          total_input_tokens: u.input || 0,
          total_output_tokens: u.output || 0,
          total_cost_usd: u.cost_usd || 0,
        };
      },
      loadConfig: async () => {
        let catalog = {};
        try { catalog = await _loadPlatformCatalog(); } catch (_) { /* Existing agent rows remain manageable if catalog fetch is unavailable. */ }
        const catalogPlatformRows = [
          ...(Array.isArray(catalog?.available_platform_models) ? catalog.available_platform_models : []),
          ...(Array.isArray(catalog?.entries) ? catalog.entries : []),
        ].filter(row => row && row.catalog_origin === 'platform');
        let roster = agentCfg.multi_providers || [];
        // Handle legacy format where multi_providers was stored as { providers: [...] }
        // instead of a plain array (pre-existing bug in saveRoster).
        if (!Array.isArray(roster) && roster && Array.isArray(roster.providers)) {
          roster = roster.providers;
        }
        // Platform runtime records carry provider/model data server-side so an
        // agent can execute them. Project a selected platform row back to its
        // safe user-catalog alias before it reaches this user-facing table.
        const platformById = new Map(catalogPlatformRows
          .map(row => [String(row.entry_id || ''), row]));
        roster = roster.map((row) => {
          const platform = platformById.get(String((row || {}).entry_id || ''));
          // MODEL-NAME-LEVELS: the row keeps the caller's own display name (the
          // server stores the catalog name when they have set none) and gains the
          // catalog's own model name as the level-1 label.
          return platform ? {
            ...row,
            entry_id: platform.entry_id,
            display_name: row.display_name || platform.display_name || '',
            catalog_name: platform.catalog_name || platform.display_name || '',
            credit_input_per_million: platform.credit_input_per_million,
            credit_output_per_million: platform.credit_output_per_million,
            provider: '', auth_mode: 'catalog', base_url: '', model: platform.model,
            catalog_origin: 'platform',
          } : { ...row, catalog_origin: 'byo' };
        });
        return {
          provider: '', auth_mode: 'api_key', base_url: '', api_key: '',
          providerConfigs: agentCfg.providerConfigs || {},
          roster,
          role_assignments: agentCfg.role_assignments || {},
          default_entry_id: String(catalog?.default_entry_id || ''),
        };
      },
      saveRoster: async ({ providers, role_assignments = {} }) => {
        agentCfg.multi_providers = providers.map((provider) => {
          const {
            _uid, credential_email, credential_plan_type, credential_suffix,
            credential_configured, enabled, use_for_image, use_for_image_out,
            use_for_system, use_for_voice,
            // MODEL-NAME-LEVELS: the catalog model name is a projection of the
            // catalog lookup, not roster data — it is re-derived on every load.
            catalog_name,
            ...persisted
          } = provider;
          return persisted;
        });
        agentCfg.role_assignments = { ...role_assignments };
        // A non-empty roster is an explicit per-agent override (its own models,
        // or role overrides materialized from inherited rows). Flip use_default
        // off so the backend honours it — an agent that was pure-inherit (no
        // roster, use_default: true) would otherwise silently ignore the first
        // override row it saves. An EMPTY roster keeps the previous use_default
        // (the stale-pin guard below already resets to inherit when a pinned
        // model is removed).
        if (providers.length && agentCfg.use_default !== false) {
          agentCfg.use_default = false;
        }
        // Remove the historical second source of truth. Runtime derives the
        // active model exclusively from this roster's Standard role.
        ['entry_id', 'provider', 'auth_mode', 'base_url', 'api_key', 'model',
          'text_capable', 'image_capable', 'image_out_capable',
          'voice_capable', 'high_effort_capable', 'routing_mode'].forEach(k => delete agentCfg[k]);
        return persistLlm().then(() => {
          const saved = agent.llm_config?.multi_providers || [];
          reconcileSavedModelRows(providers, saved);
          agentCfg.multi_providers = providers;
          return { providers };
        });
      },
      // MODEL-DEFAULT-ACCOUNT: the "Default for new agents" line on an
      // individual agent's Model page edits the caller's ACCOUNT-level catalog
      // default (app/api/entitlements.py user_catalog_default_entry_id — the
      // model a newly created agent starts on). That account write lane needs
      // the full entry set, so fetch the live catalog and replay it with only
      // the default changed; the agent's own roster must never be sent here or
      // it would corrupt the account catalog. Keep the payload shape in sync
      // with _userCatalogModelAdapter().saveRoster in
      // app/settings/agent-settings/agent-settings.js.
      saveDefaultEntryId: async (entryId) => {
        const catalog = await _loadPlatformCatalog();
        const entries = Array.isArray(catalog?.entries) ? catalog.entries : [];
        const platformEntries = entries.filter(row => row && row.catalog_origin === 'platform');
        const platform_entry_ids = platformEntries.map(row => String(row.entry_id || '')).filter(Boolean);
        const platform_aliases = {};
        platformEntries.forEach((row) => {
          const stored = String(row.display_name || '').trim();
          const catalogName = String(row.catalog_name || '').trim();
          platform_aliases[String(row.entry_id || '')] = stored && stored !== catalogName ? stored : '';
        });
        const byoEntries = entries
          .filter(row => row && row.catalog_origin !== 'platform' && row.catalog_entry_source !== 'agent')
          .map((row) => {
            const { catalog_origin, catalog_name, ...rest } = row;
            return rest;
          });
        const response = await fetch('/api/v1/entitlements/me/model-catalog', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({
            entries: byoEntries,
            platform_entry_ids,
            platform_aliases,
            default_entry_id: entryId || '',
          }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload?.detail?.message || payload?.detail || `HTTP ${response.status}`);
        return { default_entry_id: String(payload?.default_entry_id || '') };
      },
      // The single-agent picker mirrors All Agents' safe catalog presentation,
      // while this adapter's saveRoster above remains agent-local.
      loadPresets: async () => {
        let providers = {};
        try {
          const response = await fetch(apiPath('/admin/settings/providers'), { headers: { ...authHeaders() } });
          if (response.ok) providers = await response.json();
        } catch (_) { /* The existing provider fallback remains available below. */ }
        return { webagent_catalog: { name: 'WebAgent Catalog', base_url: '' }, ...providers };
      },
      fetchModels: async ({ provider, api_key, base_url }) => {
        if (provider === 'webagent_catalog') {
          return { models: platformCatalogModels.map(row => ({
            ...row,
            id: row.entry_id,
            // MODEL-CATALOG-IDENTITY: picker labels use the catalog name only —
            // never the real "provider / model" string.
            name: row.display_name || 'WebAgent Catalog model',
          })) };
        }
        if (provider === 'openai_subscription') {
          try {
            const response = await fetch(`/api/v1/openai-subscription/models?scope=agent&agent_id=${encodeURIComponent(agent.id || '')}`, { headers: { ...authHeaders() } });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return await response.json();
          } catch (error) { return { models: [], error: error.message }; }
        }
        const params = new URLSearchParams({ provider: provider || '' });
        if (api_key && base_url) { params.set('api_key', api_key); params.set('base_url', base_url); }
        try {
          const response = await fetch(apiPath(`/admin/settings/models?${params.toString()}`), { headers: { ...authHeaders() } });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return await response.json();
        } catch (error) { return { models: [], error: error.message }; }
      },
      // Per-agent roster-role prompt injections (grep ROLE-DIRECTIVE-INJECT):
      // stored in THIS agent's llm_config.model_directives and saved through
      // the same agent PUT as the roster (persistLlm) — which the backend
      // gates to the agents administrator (_is_agent_admin). The UI opens the
      // editor read-only for non-admins via directivesEditable.
      loadDirectives: async () => (agentCfg.model_directives || {}),
      saveDirectives: async (directives) => {
        agentCfg.model_directives = directives;
        return persistLlm();
      },
      subscriptionRequest: (path, options = {}) => {
        const joiner = path.includes('?') ? '&' : '?';
        const scoped = `${path}${joiner}scope=agent&agent_id=${encodeURIComponent(agent.id || '')}`;
        return fetch(scoped, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
      },
      fetchProviderCatalog: async () => {
        try {
          const r = await fetch(apiPath('/admin/settings/providers'));
          if (r.ok) { const d = await r.json(); if (d && Object.keys(d).length) return d; }
        } catch (_) {}
        return {
          openrouter:  { name: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1' },
          openai:      { name: 'ChatGPT API',       base_url: 'https://api.openai.com/v1' },
          openai_subscription: { name: 'ChatGPT Subscription', base_url: 'codex://chatgpt', auth_mode: 'chatgpt' },
          gemini:      { name: 'Google Gemini',    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai' },
          groq:        { name: 'Groq',             base_url: 'https://api.groq.com/openai/v1' },
          together:    { name: 'Together AI',       base_url: 'https://api.together.xyz/v1' },
          deepseek:    { name: 'DeepSeek',          base_url: 'https://api.deepseek.com/v1' },
          mistral:     { name: 'Mistral AI',        base_url: 'https://api.mistral.ai/v1' },
          fireworks:   { name: 'Fireworks AI',      base_url: 'https://api.fireworks.ai/inference/v1' },
          xai:         { name: 'xAI (Grok)',        base_url: 'https://api.x.ai/v1' },
          perplexity:  { name: 'Perplexity',        base_url: 'https://api.perplexity.ai' },
          ollama:      { name: 'Ollama (local)',    base_url: 'http://localhost:11434/v1' },
          deepinfra:   { name: 'DeepInfra',         base_url: 'https://api.deepinfra.com/v1/openai' },
          lmstudio:    { name: 'LM Studio (local)', base_url: 'http://localhost:1234/v1' },
        };
      },
    };

    const analytics = initModelAnalytics(analyticsHost, {
      fetchFn: (url, options = {}) => fetch(url, {
        ...options,
        headers: { ...authHeaders(), ...(options.headers || {}) },
      }),
      apiPath,
      agentId: agent.id || '',
      description: 'Compare the daily usage of every model used with this agent.',
    });

    mountModelTable(host, {
      adapter,
      // This component owns only the persisted roster for this agent.
      // User-catalog mode supplies the WebAgent Catalog default/search/credit
      // presentation without making this roster read-only or global.
      agentLocalRoster: true,
      userCatalog: true,
      subscriptionScope: 'agent',
      fetchFn: (url, options = {}) => fetch(url, {
        ...options,
        headers: { ...authHeaders(), ...(options.headers || {}) },
      }),
      subscriptionScope: 'agent',
      directivesEditable: _userIsAdmin,
      directivePresentation: 'inline',
      onModelChange: () => analytics?.reload(),
      // MODEL-LEGEND-ALIAS: feed the chart above this table the roster's display
      // names so its legend labels each model the way these rows do.
      onRoster: (providers) => analytics?.setModelLabels(providers),
      // MODEL-ROW-FOCUS: opening a saved row narrows the chart above to that model;
      // closing the row (or reloading the roster) hands the whole group back.
      onModelFocus: (model) => analytics?.setModelFocus(model),
      advancedExtra: (body) => {
        const wrap = document.createElement('div'); wrap.className = 'ac-row';
        const head = document.createElement('div'); head.className = 'ac-ability-row';
        const label = document.createElement('span'); label.className = 'ac-ability-label';
        label.innerHTML = '<span class="ac-ability-name">Reset Usage Counters</span>'
          + '<span class="ac-ability-desc">Clear your usage metrics with this agent (the tokens and cost shown in the model table above — your own, not other users\').</span>';
        const chev = document.createElement('span'); chev.className = 'ac-row-chevron';
        chev.innerHTML = icon('chevron-right', { size: 16 });
        head.appendChild(label); head.appendChild(chev);

        const inner = document.createElement('div'); inner.className = 'ac-ability-body';
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;';
        const sel = document.createElement('select'); sel.className = 'ac-input ac-config-sel';
        ['all','input','output','cost'].forEach(v => {
          const o = document.createElement('option'); o.value = v;
          o.textContent = {all:'All (input + output + cost)', input:'Input Tokens only', output:'Output Tokens only', cost:'Cost only'}[v];
          sel.appendChild(o);
        });
        const btn = document.createElement('button'); btn.className = 'ac-btn ac-btn-ghost';
        btn.textContent = 'Reset'; btn.style.color = 'var(--danger)';
        const msg = document.createElement('span'); msg.style.cssText = 'font-size:11px;color:var(--fg-3);';
        row.appendChild(sel); row.appendChild(btn); row.appendChild(msg);
        inner.appendChild(row);

        wrap.appendChild(head); wrap.appendChild(inner);
        head.addEventListener('click', () => wrap.classList.toggle('expanded'));
        body.appendChild(wrap);

        btn.addEventListener('click', async () => {
          btn.disabled = true; msg.textContent = 'Resetting…';
          try {
            const headers = {'Content-Type':'application/json', ...authHeaders()};
            const res = await fetch(`/admin/settings/agent-usage/reset?agent_id=${encodeURIComponent(agent.id)}&scope=${sel.value}`, { method:'POST', headers });
            const data = await res.json();
            if (data.ok) { msg.textContent = '✓ Reset complete'; setTimeout(() => msg.textContent = '', 3000); }
            else { msg.textContent = `✗ ${data.error || 'Failed'}`; }
          } catch (e) { msg.textContent = `✗ ${e.message}`; }
          btn.disabled = false;
        });
      },
    });
  }

  // ── Chat modes ─────────────────────────────────────────────────────────────
  // Each mode owns a permission policy plus a system-prompt injection.
  // Gates below independently control scoped guidance and enforcement.
  if (agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat' && agent.engine !== 'codex') {
    const g = _group(modesTopic, 'sliders-horizontal', 'Chat modes',
      'Modes appear in the chat footer selector. Read-only modes can research and produce proposals or plans, but cannot execute mutating tools. Write-capable modes can make changes. Each mode owns its prompt injection; independently enabled gates below add scoped guidance and enforcement.');
    const list = _cfgList(g);
    const fallbackModes = [
      { id:'ask', label:'Ask', description:'Research freely and deliver a proposal without executing it.', permission_policy:'read_only', prompt:'', builtin:true },
      { id:'plan', label:'Plan', description:'Investigate deeply and deliver an execution-ready plan.', permission_policy:'read_only', prompt:'', builtin:true },
      { id:'auto', label:'Auto', description:'Work autonomously with write-capable permissions.', permission_policy:'write', prompt:'', builtin:true },
    ];
    let modes = structuredClone(Array.isArray(agent.execution_modes) && agent.execution_modes.length ? agent.execution_modes : fallbackModes);
    let cur = modes.some(m => m.id === agent.default_execution_mode) ? agent.default_execution_mode : 'ask';
    const { ctrl, descEl } = _cfgRow(list, 'Default mode', (modes.find(m => m.id === cur) || {}).description || '');
    const sel = document.createElement('select');
    sel.className = 'ac-input ac-input-sm ac-config-sel'; sel.dataset.field = 'default_execution_mode';
    const fillDefaultSelect = () => {
      sel.innerHTML = '';
      modes.forEach(mode => {
        const o = document.createElement('option'); o.value = mode.id; o.textContent = mode.label || mode.id;
        if (mode.id === cur) o.selected = true; sel.appendChild(o);
      });
    };
    fillDefaultSelect();
    if (!isEditable) sel.disabled = true;
    ctrl.appendChild(sel);
    if (isEditable) {
      sel.addEventListener('change', async () => {
        const selected = sel.value;
        if (descEl) descEl.textContent = (modes.find(m => m.id === selected) || {}).description || '';
        if (selected === cur) return;
        const previous = cur;
        sel.disabled = true;
        const ok = await _saveCfg(agent, { default_execution_mode: selected }, ctrl);
        sel.disabled = false;
        if (ok) cur = selected;
        else { sel.value = previous; if (descEl) descEl.textContent = (modes.find(m => m.id === previous) || {}).description || ''; }
      });
    }

    // ── Gate catalog (metadata['gates']) ─────────────────────────────────────
    // DOM BOUNDARY: Gates is a standalone `.ac-list` appended after the Modes
    // editor. It is a sibling of the Modes list, not a child of Baseline details
    // or the Abilities panel. Each row mirrors the ability catalog contract:
    // `.ac-row > .ac-ability-row + .ac-ability-body`. The mode picker is NOT a
    // row descendant: the row's toggle opens a body-level `#files-floating-menu`
    // popover BELOW the toggle (the anchor) — the same shared component and
    // anchor the ability rows' availability picker uses. (GATES-SCOPE-POPOVER:
    // mirrors AGENT-ABILITY-TABLE; replaces the old chevron + inline
    // "Assign to modes" scope panel.)
    // Closing Message and Message visibility are configured from Identity & Design.
    // Keep only gates whose controls belong with per-mode configuration here.
    const gateMenu = (agent.gates && Array.isArray(agent.gates.types))
      ? agent.gates.types.filter(type => !['closing_message', 'message_visibility'].includes(type.id))
      : [];
    const persistedGateByType = new Map(((agent.gates && agent.gates.instances) || []).map(row => [row.type, row]));
    // App-level params (e.g. the closing_message flag taxonomy) are configured
    // once for the whole app, so they must never be captured from — or written
    // back into — an agent's own row. Stripping them here keeps the per-agent
    // save payload free of any `level: 'app'` param even when a legacy row on
    // disk still carries one. (The agent write path in app/api/agents.py enforces
    // the same rule server-side.)
    const appLevelKeys = type => Object.entries((type && type.params) || {})
      .filter(([, definition]) => String((definition || {}).level || '') === 'app')
      .map(([key]) => key);
    const agentLevelParams = (type, params) => {
      const drop = new Set(appLevelKeys(type));
      return Object.fromEntries(Object.entries(params || {}).filter(([key]) => !drop.has(key)));
    };
    const gateInstances = gateMenu.map(type => {
      const row = persistedGateByType.get(type.id) || {};
      // Gate state is derived from assigned modes. A disabled legacy row must
      // start with no assignments; an enabled row with no explicit scope means
      // every current mode is selected.
      const configuredScope = Array.isArray(row.scope) && row.scope.length ? [...row.scope] : null;
      return { type:type.id, enabled:row.enabled === true, params:agentLevelParams(type, row.params), scope:row.enabled === true ? configuredScope : [] };
    });
    const gateTypeOf = id => gateMenu.find(type => type.id === id) || { id, display_name:id, description:'', phase:'prompt', params:{} };
    const gatePayload = () => gateInstances.map(gate => {
      const row = { type:gate.type, enabled:gate.enabled === true };
      if (gate.scope && gate.scope.length) row.scope = [...gate.scope];
      const params = Object.fromEntries(Object.entries(agentLevelParams(gateTypeOf(gate.type), gate.params)).filter(([, value]) => value !== '' && value !== null && value !== undefined));
      if (Object.keys(params).length) row.params = params;
      return row;
    });
    const persistedCatalogRows = rows => rows.map(row => ({ type:row.type, enabled:row.enabled === true, ...(row.scope && row.scope.length ? { scope:row.scope } : {}), ...(row.params && Object.keys(row.params).length ? { params:row.params } : {}) }));
    const saveGates = anchor => _saveCfgVerified(agent, { gates:{ schema_version:1, gates:gatePayload() } }, anchor,
      persisted => _matchesPersisted(persistedCatalogRows((persisted.gates && persisted.gates.instances) || []), gatePayload()));
    const modeIds = () => modes.filter(mode => !mode._deleted).map(mode => mode.id);
    const gatesHeading = document.createElement('div');
    gatesHeading.className = 'ac-config-section-title';
    gatesHeading.style.cssText = 'margin:16px 0 6px;font-size:13px;font-weight:650;color:var(--fg-1);';
    gatesHeading.textContent = 'Gates';
    const gatesList = document.createElement('div');
    gatesList.className = 'ac-list ac-gates-catalog';
    gatesList.dataset.gatesCatalog = 'true';
    const renderGatesCatalog = () => {
      gatesList.innerHTML = '';
      if (!gateMenu.length) {
        const empty = document.createElement('div'); empty.className = 'ac-ability-row'; empty.textContent = 'No Markdown gate descriptors are installed.'; gatesList.appendChild(empty); return;
      }
      gateInstances.forEach(gate => {
        const type = gateTypeOf(gate.type);
        const card = document.createElement('div'); card.className = 'ac-row ac-gate-row' + (gate.enabled ? ' enabled' : ''); card.dataset.gateType = gate.type;
        const header = document.createElement('div'); header.className = 'ac-ability-row ac-gate-head' + (gate.enabled ? ' ac-ability-enabled' : '');
        const glyph = document.createElement('span'); glyph.className = 'ac-ability-icon'; glyph.innerHTML = icon(type.phase === 'completion' ? 'circle-check' : type.phase === 'prompt' ? 'message-square-text' : 'shield-check', { size:'16px' });
        const label = document.createElement('div'); label.className = 'ac-ability-label';
        const name = document.createElement('div'); name.className = 'ac-ability-name'; name.textContent = type.display_name || type.id;
        const description = document.createElement('div'); description.className = 'ac-ability-desc'; description.textContent = type.description || '';
        label.append(name, description);
        // Like an Abilities availability switch, this control is a summary and
        // opener — never a direct mutation. Clicking it opens the shared
        // body-level floating panel (`openFloatingMenu`, the exact component the
        // ability rows' availability picker uses) instead of a scope section
        // under the row. Gate state is derived solely from that panel's
        // assignments. (GATES-SCOPE-POPOVER: mirrors AGENT-ABILITY-TABLE.)
        const toggleLabel = document.createElement('label'); toggleLabel.className = 'conn-toggle-wrap ac-ability-toggle-wrap'; toggleLabel.title = 'Choose assigned modes';
        const toggle = document.createElement('input'); toggle.type = 'checkbox'; toggle.className = 'conn-toggle ac-gate-toggle'; toggle.checked = gate.enabled; toggle.disabled = !isEditable; toggle.setAttribute('aria-label', `${type.display_name || type.id} assigned modes: ${gate.enabled ? 'on' : 'off'}. Choose assigned modes.`); toggle.setAttribute('aria-haspopup', 'true'); toggle.setAttribute('aria-expanded', 'false');
        const track = document.createElement('span'); track.className = 'conn-toggle-track'; toggleLabel.append(toggle, track); header.append(glyph, label, toggleLabel);

        const selectedModeIds = () => gate.scope === null ? modeIds() : [...(gate.scope || [])];
        const paintAssignmentState = () => {
          const selected = selectedModeIds();
          gate.enabled = selected.length > 0;
          toggle.checked = gate.enabled;
          toggle.setAttribute('aria-label', `${type.display_name || type.id} assigned modes: ${gate.enabled ? 'on' : 'off'}. Choose assigned modes.`);
          card.classList.toggle('enabled', gate.enabled);
          header.classList.toggle('ac-ability-enabled', gate.enabled);
        };
        const persistAssignments = async (selected) => {
          const beforeScope = gate.scope === null ? null : [...(gate.scope || [])];
          const beforeEnabled = gate.enabled;
          gate.scope = selected.length === modeIds().length && selected.length > 0 ? null : selected;
          paintAssignmentState();                             // optimistic
          _ovMarkSaving(toggleLabel);
          const ok = await saveGates(card);
          if (!ok) { gate.scope = beforeScope; gate.enabled = beforeEnabled; paintAssignmentState(); }
          _ovFlashCheck(toggleLabel, !!ok);
        };
        // The popover body, rebuilt on every open/repaint: All Modes + one
        // checkbox-style entry per mode — the ability-row menu's exact item shape
        // (`iconHtml`/`label`/`checked`/`disabled`/`keepOpen`/`action`), but
        // MULTI-select, so `keepOpen` holds the panel up while several are
        // toggled and the ✓ marks follow the live scope after each pick.
        const gateModeMenuItems = () => {
          const ids = modeIds();
          const selected = new Set(selectedModeIds());
          const allChecked = ids.length > 0 && selected.size === ids.length;
          const items = [{
            iconHtml: allChecked ? GATE_BOX_ON_SVG : GATE_BOX_OFF_SVG,
            label: 'All Modes',
            checked: allChecked,
            disabled: !isEditable,
            keepOpen: true,
            action: () => persistAssignments(allChecked ? [] : ids),
          }];
          if (ids.length) items.push({ separator: true });
          ids.forEach(modeId => {
            const on = selected.has(modeId);
            items.push({
              iconHtml: on ? GATE_BOX_ON_SVG : GATE_BOX_OFF_SVG,
              label: (modes.find(mode => mode.id === modeId) || {}).label || modeId,
              checked: on,
              disabled: !isEditable,
              keepOpen: true,
              action: () => {
                const next = new Set(selectedModeIds());
                if (next.has(modeId)) next.delete(modeId); else next.add(modeId);
                return persistAssignments([...next]);
              },
            });
          });
          return items;
        };
        toggle.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          if (!isEditable) return;
          toggle.setAttribute('aria-expanded', 'true');
          // Anchor the panel just below the VISIBLE switch, right-edge aligned —
          // the same geometry the ability rows' availability picker uses (menu
          // width 180). The input itself is a 0×0 visually-hidden checkbox, so
          // measure the wrapping control, not the input.
          const rect = toggleLabel.getBoundingClientRect();
          openFloatingMenu(gateModeMenuItems, rect.bottom + 4, rect.right - 180);
        });
        paintAssignmentState();

        const body = document.createElement('div'); body.className = 'ac-ability-body ac-gate-body';
        const content = document.createElement('div'); content.style.cssText = 'display:flex;flex-direction:column;gap:10px;';
        const help = document.createElement('div'); help.style.cssText = 'font-size:11px;color:var(--fg-3);line-height:1.45;'; help.textContent = type.phase === 'prompt' ? 'Prompt guidance only.' : `This gate runs during the ${type.phase.replace('_', ' ')} phase.`; content.appendChild(help);
        // Assignment controls intentionally live only in the toggle-opened
        // popover. The expanded body is reserved for gate explanation and settings.
        // Each gate's body is TAILORED to that gate: it renders only the params
        // the descriptor declares for the AGENT level. A param marked
        // `level: 'app'` (the closing_message gate's flag taxonomy) is configured
        // once for the whole app, so it is deliberately not offered here — one
        // vocabulary governs every agent.
        // Params render by their declared type. Numbers (limit/cap style params)
        // keep the bounded number input; `string` params — here the closing_message
        // gate's notification and format guidance — are free text, multiline when
        // the descriptor says so. Forcing a string through the number input would
        // silently coerce it to 0.
        // (GATE-PARAM-TYPES)
        const appLevelParams = [];
        const agentParamRows = [];
        const paramBounds = new Map();
        const booleanParams = new Set();
        Object.entries(type.params || {}).forEach(([key, definition]) => {
          if (String(definition.level || '') === 'app') {
            appLevelParams.push(definition.label || key);
            return;
          }
          const declaredType = String(definition.type || '');
          const isText = declaredType === 'string';
          const isBoolean = declaredType === 'boolean';
          if (isBoolean) booleanParams.add(key);
          const row = {
            key,
            label: definition.label || key,
            hint: definition.hint || '',
            type: isText ? (definition.multiline ? 'textarea' : 'text') : (isBoolean ? 'boolean' : 'number'),
          };
          if (isText) {
            if (definition.multiline) row.rows = 4;
            row.default = definition.default ?? '';
          } else if (isBoolean) {
            row.default = definition.default ?? false;
          } else {
            row.min = definition.min ?? 0;
            row.max = definition.max ?? 999;
            row.default = definition.default ?? 0;
            paramBounds.set(key, { min: row.min, max: row.max });
          }
          agentParamRows.push(row);
        });
        // STACKED rows — the field NAME on its own line, the box on the next
        // (full width), and its description under that, click-to-expand when it
        // overflows. Built by dom-utils `buildGateParamRowsHtml` so an agent's
        // gate body reads EXACTLY like the app-level Gates table: one gate, one
        // layout, on both surfaces. (GATES-PARAM-LAYOUT)
        if (agentParamRows.length) {
          const rows = document.createElement('div');
          rows.innerHTML = buildGateParamRowsHtml(
            { settings: agentParamRows }, gate.params || {}, 'ac-gate-input', 'data-gate-key',
          );
          content.appendChild(rows);
          rows.querySelectorAll('.ac-gate-input').forEach(input => {
            const key = input.getAttribute('data-gate-key');
            input.disabled = !isEditable;
            input.addEventListener('input', () => {
              const bounds = paramBounds.get(key);
              gate.params[key] = booleanParams.has(key)
                ? input.value === 'true'
                : (bounds
                  ? Math.max(bounds.min, Math.min(bounds.max, parseInt(input.value, 10) || 0))
                  : input.value);
            });
          });
          wireGateParamRows(rows);
        }
        if (appLevelParams.length) {
          // Signpost where the app-wide knobs live rather than silently omitting
          // them: an agent owner hunting for the flag list needs to know.
          const appHint = document.createElement('div');
          appHint.style.cssText = 'font-size:11px;color:var(--fg-3);line-height:1.45;';
          appHint.textContent = `App-wide, set in App Settings → Agent tools & abilities → Gates: ${appLevelParams.join(', ')}.`;
          content.appendChild(appHint);
        }
        if (isEditable) { const save = document.createElement('button'); save.type = 'button'; save.className = 'ac-btn ac-btn-ghost'; save.textContent = 'Save gate settings'; save.addEventListener('click', async () => { save.disabled = true; const ok = await saveGates(card); save.disabled = false; if (ok) { save.textContent = 'Saved'; setTimeout(() => { save.textContent = 'Save gate settings'; }, 1500); } }); content.appendChild(save); }
        header.addEventListener('click', event => { if (!event.target.closest('.conn-toggle-wrap')) card.classList.toggle('expanded'); });
        body.appendChild(content); card.append(header, body); gatesList.appendChild(card);
      });
    };

    const editor = document.createElement('div');
    editor.style.cssText = 'display:flex;flex-direction:column;gap:8px;padding:10px 0 2px;';
    g.appendChild(editor);
    // Mode cards save explicitly. This placeholder keeps the structural add/remove
    // paths from issuing an automatic request while each card owns its own dirty
    // state and SAVE CHANGES button below.
    const saveModes = () => {};

    const renderModes = () => {
      editor.innerHTML = '';
      modes.forEach((mode, index) => {
        const card = document.createElement('details');
        card.style.cssText = 'border:var(--border-width) solid var(--border);border-radius:8px;padding:8px 10px;background:var(--bg-1);';
        const summary = document.createElement('summary');
        summary.style.cssText = 'cursor:pointer;display:flex;align-items:center;gap:8px;font-size:12px;font-weight:650;';
        const summaryName = document.createElement('span'); summaryName.textContent = mode.label || mode.id;
        const badge = document.createElement('span');
        badge.style.cssText = 'font-size:10px;color:var(--fg-3);font-weight:500;';
        badge.textContent = mode.permission_policy === 'write' ? 'Write-capable' : 'Read-only';
        summary.appendChild(summaryName); summary.appendChild(badge); card.appendChild(summary);

        const fields = document.createElement('div');
        fields.style.cssText = 'display:grid;grid-template-columns:minmax(120px,1fr) minmax(140px,1fr);gap:8px;margin-top:10px;';
        let markDirty = () => {};
        const field = (label, input, wide=false) => {
          const wrap = document.createElement('label');
          wrap.style.cssText = `display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--fg-3);${wide ? 'grid-column:1/-1;' : ''}`;
          const text = document.createElement('span'); text.textContent = label;
          wrap.appendChild(text); wrap.appendChild(input); fields.appendChild(wrap); return input;
        };
        const name = document.createElement('input'); name.className = 'ac-input'; name.value = mode.label || '';
        field('Name', name);
        const policy = document.createElement('select'); policy.className = 'ac-input';
        [['read_only','Read-only'],['write','Write-capable']].forEach(([v,t]) => { const o=document.createElement('option'); o.value=v; o.textContent=t; o.selected=mode.permission_policy===v; policy.appendChild(o); });
        field('Permission policy', policy);
        const description = document.createElement('input'); description.className = 'ac-input'; description.value = mode.description || '';
        field('Footer description', description, true);
        const prompt = document.createElement('textarea'); prompt.className = 'ac-input'; prompt.rows = 6; prompt.value = mode.prompt || '';
        field('Prompt injection', prompt, true);
        if (!mode.builtin) {
          const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'ac-btn ac-btn-ghost'; remove.textContent = 'Remove mode'; remove.style.color = 'var(--danger)';
          remove.addEventListener('click', () => {
            // Removal is a draft change too: keep the card in place until its
            // own SAVE CHANGES request is confirmed by the backend.
            mode._deleted = true;
            summaryName.textContent = `${mode.label || mode.id} (will be removed)`;
            fields.querySelectorAll('input,textarea,select,button:not([data-mode-card-save])').forEach(el => { el.disabled = true; });
            remove.disabled = false;
            remove.textContent = 'Will be removed on save';
            markDirty();
          });
          fields.appendChild(remove);
        }
        const sync = () => {
          mode.label = name.value.trim() || mode.id;
          mode.description = description.value.trim(); mode.permission_policy = policy.value;
          mode.prompt = prompt.value;
          summaryName.textContent = mode.label; badge.textContent = mode.permission_policy === 'write' ? 'Write-capable' : 'Read-only';
          if (mode.id === cur && descEl) descEl.textContent = mode.description;
          fillDefaultSelect(); markDirty();
        };
        [name, description, prompt].forEach(el => el.addEventListener('input', sync));
        policy.addEventListener('change', sync);
        markDirty = _attachCardSaveButton(card, fields, isEditable, async () => {
          // The card owns the explicit save. Send the complete mode list because
          // that is the API's atomic representation, then require the server to
          // echo it before showing the success flash.
          const modesPayload = structuredClone(modes
            .filter(candidate => !candidate._deleted)
            .map(({ _new, _deleted, ...candidate }) => candidate));
          const saved = await _saveCfgVerified(agent, { execution_modes: modesPayload }, card,
            persisted => _matchesPersisted(persisted.execution_modes, modesPayload));
          if (saved) {
            delete mode._new;
            if (mode._deleted) {
              modes.splice(index, 1);
              if (cur === mode.id) { cur = 'ask'; }
              fillDefaultSelect();
              setTimeout(renderModes, 0);
            }
          }
          return !!saved;
        });
        if (mode._new) markDirty();
        if (!isEditable) fields.querySelectorAll('input,textarea,select,button').forEach(el => { el.disabled = true; });
        card.appendChild(fields); editor.appendChild(card);
      });
      if (isEditable) {
        const add = document.createElement('button'); add.type = 'button'; add.className = 'ac-btn ac-btn-ghost'; add.innerHTML = `${icon('plus', { size:'14px' })} Add mode`;
        add.addEventListener('click', () => {
          let n = 1; while (modes.some(m => m.id === `custom-${n}`)) n++;
          modes.push({ id:`custom-${n}`, label:'New mode', description:'', permission_policy:'read_only', prompt:'', builtin:false, _new:true });
          fillDefaultSelect(); renderModes();
        });
        editor.appendChild(add);
      }
    };
    renderModes();
    // Keep Gates structurally after the complete Modes editor.
    g.appendChild(gatesHeading);
    g.appendChild(gatesList);
    renderGatesCatalog();
  }

  // ── Limits ────────────────────────────────────────────────────────────────
  // Always present (read-only number fields for non-editable templates); save
  // wiring only for editable, non-mock agents. Max Concurrent Tools folds in here.
  {
    const g = _group(runtimeTopic, 'gauge', 'Limits');
    const list = _cfgList(g);
    const L = [
      { field: 'max_turn_count', label: 'Max Turn Count', value: agent.max_turn_count != null ? agent.max_turn_count : 9999, placeholder: '9999', min: 0, max: 99999, hint: 'Caps the LLM → tool → LLM cycles per response.' },
      { field: 'max_wall_seconds', label: 'Wall Clock (seconds)', value: agent.max_wall_seconds != null ? agent.max_wall_seconds : '', placeholder: '0 (off)', min: 0, max: 86400, step: 1, hint: 'Limits total real time for one response.' },
      { field: 'max_tokens', label: 'Max Tokens', value: agent.max_tokens != null ? agent.max_tokens : 8096, placeholder: '8096', min: 1, step: 1, hint: 'Maximum output tokens per LLM response.' },
      { field: 'max_identical_tool_calls', label: 'Identical Tool Calls', value: agent.max_identical_tool_calls != null ? agent.max_identical_tool_calls : '', placeholder: '0 (off)', min: 0, max: 9999, step: 1, hint: 'Limits how many times the agent can call the same tool with same args.' },
      { field: 'max_stall_strikes', label: 'Stall Strikes', value: agent.max_stall_strikes != null ? agent.max_stall_strikes : '', placeholder: '0 (off)', min: 0, max: 99, step: 1, hint: 'After this many stall guard detections, the agent stops and asks for clarification.' },
    ];

    L.forEach(cfg => {
      const { ctrl } = _cfgRow(list, cfg.label, cfg.hint);
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.dataset.field = cfg.field; inputEl.value = cfg.value; inputEl.placeholder = cfg.placeholder;
      inputEl.min = cfg.min; inputEl.max = cfg.max; if (cfg.step) inputEl.step = cfg.step;
      if (!isEditable) inputEl.readOnly = true;
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (isEditable) {
        const saveLimit = _debounced(() => {
          const raw = inputEl.value;
          const val = cfg.field === 'max_wall_seconds'
            ? (raw === '' ? null : (isNaN(parseFloat(raw)) ? null : parseFloat(raw)))
            : (raw === '' ? 0 : (parseInt(raw, 10) || 0));
          _saveCfg(agent, { [cfg.field]: val }, ctrl);
        });
        inputEl.addEventListener('input', saveLimit); inputEl.addEventListener('blur', () => saveLimit.flush());
      }
    });

    // Resume tail messages — stored in metadata, 0 = use app default (32).
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Resume Tail Messages', 'How many recent messages to replay when a session resumes. 0 = app default (32).');
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.min = 0; inputEl.max = 999; inputEl.step = 1;
      inputEl.value = (agent.resume_tail_messages != null && agent.resume_tail_messages > 0) ? agent.resume_tail_messages : '';
      inputEl.placeholder = '0 (default 32)';
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (isEditable) {
        const saveResume = _debounced(() => {
          const raw = inputEl.value;
          const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
          _saveCfg(agent, { resume_tail_messages: val }, ctrl);
        });
        inputEl.addEventListener('input', saveResume); inputEl.addEventListener('blur', () => saveResume.flush());
      }
    }

    // Auto-resume knobs — stored in metadata; 0/blank = use the app global.
    // The attempt count is bounded by the app-global ceiling
    // (run_max_resume_attempts, edited on App Settings → App Functions); the
    // server clamps to it as well, so a stale oversized value can't exceed it.
    if (isEditable) {
      const _appSettings = window.__agentsAppSettingsData || {};
      const _ceilingRaw = parseInt(_appSettings.run_max_resume_attempts, 10);
      const ceiling = (!isNaN(_ceilingRaw) && _ceilingRaw > 0) ? _ceilingRaw : 6;

      const { ctrl } = _cfgRow(list, 'Resume Attempts', `How many times a crashed run is auto-resumed before it is marked failed. 0 = app default; capped at the app limit (${ceiling}).`);
      const raEl = document.createElement('input'); raEl.type = 'number';
      raEl.className = 'ac-input ac-input-sm ac-config-num';
      raEl.min = 0; raEl.max = ceiling; raEl.step = 1;
      raEl.value = (agent.resume_max_attempts != null && agent.resume_max_attempts > 0) ? agent.resume_max_attempts : '';
      // Placeholder names the app-global ceiling (blank = inherit it); the
      // stepper locks at that ceiling with a "Global limit is N" bubble.
      raEl.placeholder = `${ceiling} (global limit)`;
      _wireGlobalLimitStepper(raEl, ctrl.appendChild(_wrapNumberStepper(raEl)), ceiling);
      const saveResumeMax = _debounced(() => {
        const raw = raEl.value;
        const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
        _saveCfg(agent, { resume_max_attempts: val }, ctrl);
      });
      raEl.addEventListener('input', saveResumeMax); raEl.addEventListener('blur', () => saveResumeMax.flush());

      // Per-error-type budget: how many of the attempts one single cause may use
      // before the run stops early. 0/blank = app default; clamped to the global
      // per-type ceiling by the runner. Mirrors the total budget's field above.
      const _pcRaw = parseInt(_appSettings.run_max_resume_attempts_per_cause, 10);
      const perCauseCeiling = (!isNaN(_pcRaw) && _pcRaw > 0) ? _pcRaw : 3;
      const { ctrl: rcCtrl } = _cfgRow(list, 'Resume Attempts (per error type)', `How many of those attempts a SINGLE error type (crash, server restart, frozen, zombie, empty response) may use before the run stops early. 0 = app default; capped at the app limit (${perCauseCeiling}).`);
      const rcEl = document.createElement('input'); rcEl.type = 'number';
      rcEl.className = 'ac-input ac-input-sm ac-config-num';
      rcEl.min = 0; rcEl.max = perCauseCeiling; rcEl.step = 1;
      rcEl.value = (agent.resume_max_attempts_per_cause != null && agent.resume_max_attempts_per_cause > 0) ? agent.resume_max_attempts_per_cause : '';
      rcEl.placeholder = `${perCauseCeiling} (global limit)`;
      _wireGlobalLimitStepper(rcEl, rcCtrl.appendChild(_wrapNumberStepper(rcEl)), perCauseCeiling);
      const saveResumePerCause = _debounced(() => {
        const raw = rcEl.value;
        const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
        _saveCfg(agent, { resume_max_attempts_per_cause: val }, rcCtrl);
      });
      rcEl.addEventListener('input', saveResumePerCause); rcEl.addEventListener('blur', () => saveResumePerCause.flush());

      const { ctrl: backoffCtrl } = _cfgRow(list, 'Resume Backoff', 'Base seconds for the exponential wait between resume attempts (doubles each try, capped at 30 min). 0 = app default (30s).');
      const rbEl = document.createElement('input'); rbEl.type = 'number';
      rbEl.className = 'ac-input ac-input-sm ac-config-num';
      rbEl.min = 0; rbEl.max = 86400; rbEl.step = 1;
      rbEl.value = (agent.resume_backoff_seconds != null && agent.resume_backoff_seconds > 0) ? agent.resume_backoff_seconds : '';
      rbEl.placeholder = '0 (default 30)';
      backoffCtrl.appendChild(_wrapNumberStepper(rbEl));
      const saveBackoff = _debounced(() => {
        const raw = rbEl.value;
        const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
        _saveCfg(agent, { resume_backoff_seconds: val }, backoffCtrl);
      });
      rbEl.addEventListener('input', saveBackoff); rbEl.addEventListener('blur', () => saveBackoff.flush());
    }

    // Closing-message REQUIREMENT-COMPLIANCE budgets — stored in metadata;
    // 0/blank = inherit the app global. NOT the resume/error counters: the
    // closing gate re-injects its instruction while the run's final message does
    // not yet meet the closing contract (flag in the taxonomy, notification above
    // 0 and within the word limit, a non-empty message) and nothing has failed.
    // Both values are clamped to the app ceilings (run_max_closing_attempts /
    // run_max_closing_attempts_per_requirement, edited on App Settings → App
    // Functions); the server clamps too, so a stale oversized value can't exceed.
    if (isEditable) {
      const _appSettings = window.__agentsAppSettingsData || {};
      const _cmaRaw = parseInt(_appSettings.run_max_closing_attempts, 10);
      const closeCeiling = (!isNaN(_cmaRaw) && _cmaRaw > 0) ? _cmaRaw : 8;
      const { ctrl: cmaCtrl } = _cfgRow(list, 'Closing Message Attempts', `How many corrective turns the run may spend getting its final message to meet the closing-message contract (a valid flag, a 1-N word notification, a non-empty message). Not error recovery. 0 = app default; capped at the app limit (${closeCeiling}).`);
      const cmaEl = document.createElement('input'); cmaEl.type = 'number';
      cmaEl.className = 'ac-input ac-input-sm ac-config-num';
      cmaEl.min = 0; cmaEl.max = closeCeiling; cmaEl.step = 1;
      cmaEl.value = (agent.closing_max_attempts != null && agent.closing_max_attempts > 0) ? agent.closing_max_attempts : '';
      cmaEl.placeholder = `${closeCeiling} (global limit)`;
      _wireGlobalLimitStepper(cmaEl, cmaCtrl.appendChild(_wrapNumberStepper(cmaEl)), closeCeiling);
      const saveClosingMax = _debounced(() => {
        const raw = cmaEl.value;
        const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
        _saveCfg(agent, { closing_max_attempts: val }, cmaCtrl);
      });
      cmaEl.addEventListener('input', saveClosingMax); cmaEl.addEventListener('blur', () => saveClosingMax.flush());

      const _cmrRaw = parseInt(_appSettings.run_max_closing_attempts_per_requirement, 10);
      const closePerReqCeiling = (!isNaN(_cmrRaw) && _cmrRaw > 0) ? _cmrRaw : 3;
      const { ctrl: cmrCtrl } = _cfgRow(list, 'Closing Message Attempts (per requirement)', `How many of those corrective turns ONE still-unmet requirement (flag, notification, message) may use before the run gives up on it and shows a partial. 0 = app default; capped at the app limit (${closePerReqCeiling}).`);
      const cmrEl = document.createElement('input'); cmrEl.type = 'number';
      cmrEl.className = 'ac-input ac-input-sm ac-config-num';
      cmrEl.min = 0; cmrEl.max = closePerReqCeiling; cmrEl.step = 1;
      cmrEl.value = (agent.closing_max_attempts_per_requirement != null && agent.closing_max_attempts_per_requirement > 0) ? agent.closing_max_attempts_per_requirement : '';
      cmrEl.placeholder = `${closePerReqCeiling} (global limit)`;
      _wireGlobalLimitStepper(cmrEl, cmrCtrl.appendChild(_wrapNumberStepper(cmrEl)), closePerReqCeiling);
      const saveClosingPerReq = _debounced(() => {
        const raw = cmrEl.value;
        const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
        _saveCfg(agent, { closing_max_attempts_per_requirement: val }, cmrCtrl);
      });
      cmrEl.addEventListener('input', saveClosingPerReq); cmrEl.addEventListener('blur', () => saveClosingPerReq.flush());
    }

    // Assertions REQUIREMENT-COMPLIANCE budget — stored in metadata; 0/blank =
    // inherit the app global. NOT the resume/error counters: the assertions gate
    // re-injects its resolution instruction while an assertion the run DECLARED
    // is still unanswered (app/agent/assertions.py) and nothing has failed.
    // Separate from the closing budget above — different requirement, counter and
    // lifecycle (the declaration beat, not the end-of-run message). Clamped to the
    // app ceiling (run_max_assertions_attempts, edited on App Settings → App
    // Functions); the server clamps too, so a stale oversized value can't exceed.
    if (isEditable) {
      const _appSettingsA = window.__agentsAppSettingsData || {};
      const _amaRaw = parseInt(_appSettingsA.run_max_assertions_attempts, 10);
      const assertCeiling = (!isNaN(_amaRaw) && _amaRaw > 0) ? _amaRaw : 4;
      const { ctrl: amaCtrl } = _cfgRow(list, 'Assertion Attempts', `How many corrective turns the run may spend answering for the assertions it declared before it may finish. Not error recovery. 0 = app default; capped at the app limit (${assertCeiling}).`);
      const amaEl = document.createElement('input'); amaEl.type = 'number';
      amaEl.className = 'ac-input ac-input-sm ac-config-num';
      amaEl.min = 0; amaEl.max = assertCeiling; amaEl.step = 1;
      amaEl.value = (agent.assertions_max_attempts != null && agent.assertions_max_attempts > 0) ? agent.assertions_max_attempts : '';
      amaEl.placeholder = `${assertCeiling} (global limit)`;
      _wireGlobalLimitStepper(amaEl, amaCtrl.appendChild(_wrapNumberStepper(amaEl)), assertCeiling);
      const saveAssertMax = _debounced(() => {
        const raw = amaEl.value;
        const val = (raw === '' || parseInt(raw, 10) === 0) ? null : (parseInt(raw, 10) || 0);
        _saveCfg(agent, { assertions_max_attempts: val }, amaCtrl);
      });
      amaEl.addEventListener('input', saveAssertMax); amaEl.addEventListener('blur', () => saveAssertMax.flush());
    }

    // Max concurrent tools — safety_policy.max_concurrent_tools (0/blank = off).
    // (Relocated from the old Tools tab; the two per-agent confirmation toggles
    // that used to sit beside it were removed as redundant with the chat "Auto"
    // execution mode. The `guardrails` loop node is still managed from the Agent
    // Loop tab; safety_policy.auto_confirm keeps its stored default.)
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Max Concurrent Tools', 'Cap how many tools run in parallel. 0 = unlimited.');
      const sp0 = (agent.safety_policy && typeof agent.safety_policy === 'object') ? agent.safety_policy : {};
      const inputEl = document.createElement('input'); inputEl.type = 'number';
      inputEl.className = 'ac-input ac-input-sm ac-config-num';
      inputEl.min = 0; inputEl.max = 20; inputEl.step = 1;
      inputEl.value = sp0.max_concurrent_tools != null ? sp0.max_concurrent_tools : '';
      inputEl.placeholder = 'unlimited';
      ctrl.appendChild(_wrapNumberStepper(inputEl));
      if (isEditable) {
        const saveMax = _debounced(() => {
          const sp = (agent.safety_policy && typeof agent.safety_policy === 'object') ? agent.safety_policy : {};
          const newSp = { ...sp };
          const mv = parseInt(inputEl.value, 10);
          if (mv > 0) newSp.max_concurrent_tools = mv; else delete newSp.max_concurrent_tools;
          _saveCfg(agent, { safety_policy: newSp }, ctrl);
        });
        inputEl.addEventListener('input', saveMax); inputEl.addEventListener('blur', () => saveMax.flush());
      }
    }
  }

  // ── Deploy & Share: sharing access ──────────────────────────────────────────
  // Public link remains editable-only. Anonymous access is configured in the
  // Visitor level of the Member access card below.
  {
    const g = _group(deployTopic, 'share-2', 'Sharing access');
    const list = _cfgList(g);

    // Public link — full-app shared chat page at /{agent_id}
    if (isEditable) {
      const { ctrl } = _cfgRow(list, 'Public link', 'Enable a public link to share with users to access your agent.');
      const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
      cb.checked = !!agent.public_link; cb.dataset.field = 'public_link';
      const track = document.createElement('span'); track.className = 'conn-toggle-track';
      wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);

      const publicUrl = `${window.location.origin}/${encodeURIComponent(agent.id)}`;
      const urlRow = _cfgRow(list, '');
      urlRow.row.classList.add('agent-public-link-url-row');
      const urlActions = document.createElement('span'); urlActions.className = 'agent-public-link-url-actions';
      const urlLink = document.createElement('a');
      urlLink.href = publicUrl; urlLink.target = '_blank'; urlLink.rel = 'noopener noreferrer';
      urlLink.className = 'agent-public-link-url'; urlLink.textContent = publicUrl;
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button'; copyBtn.className = 'agent-public-link-action';
      copyBtn.title = 'Copy public link'; copyBtn.setAttribute('aria-label', 'Copy public link');
      copyBtn.innerHTML = icon('copy', { size: '14px' });
      copyBtn.addEventListener('click', () => {
        copyText(publicUrl).then(() => {
          clearTimeout(copyBtn._copiedTimer);
          copyBtn.title = 'Copied'; copyBtn.setAttribute('aria-label', 'Public link copied');
          copyBtn.classList.add('is-copied');
          copyBtn.innerHTML = icon('check', { size: '14px' });
          _showConfigActionNotice('Copied');
          copyBtn._copiedTimer = setTimeout(() => {
            copyBtn.title = 'Copy public link'; copyBtn.setAttribute('aria-label', 'Copy public link');
            copyBtn.classList.remove('is-copied');
            copyBtn.innerHTML = icon('copy', { size: '14px' });
          }, 1200);
        }).catch(() => _showConfigActionNotice('Copy failed', true));
      });
      const qrBtn = document.createElement('button');
      qrBtn.type = 'button'; qrBtn.className = 'agent-public-link-action';
      qrBtn.title = 'Show QR code'; qrBtn.setAttribute('aria-label', 'Show QR code');
      qrBtn.innerHTML = icon('qr-code', { size: '14px' });
      qrBtn.addEventListener('click', () => showQrPopup({ url: publicUrl, anchor: qrBtn }));
      urlActions.append(urlLink, copyBtn, qrBtn);
      urlRow.ctrl.appendChild(urlActions);
      const updatePublicUrlVisibility = () => { urlRow.row.style.display = cb.checked ? '' : 'none'; };
      updatePublicUrlVisibility();

      cb.addEventListener('change', async () => {
        const requested = cb.checked;
        cb.disabled = true;
        const ok = await _saveCfg(agent, { public_link: requested }, ctrl);
        cb.disabled = false;
        if (!ok) cb.checked = !requested;
        updatePublicUrlVisibility();
      });
    }
  }

  // ── Runtime: access & triggering ────────────────────────────────────────────
  // Trigger and Trigger Key are editable-only.
  {
    const g = _group(runtimeTopic, 'zap', 'Access & triggering');
    const list = _cfgList(g);

    // Trigger + Trigger Key (editable only)
    if (isEditable) {
      const trig = _cfgRow(list, 'Trigger', 'What starts a run for this agent.');
      const trigSel = document.createElement('select');
      trigSel.className = 'ac-input ac-input-sm ac-config-sel'; trigSel.dataset.field = 'trigger_type';
      [['user_input', 'Chat'], ['webhook', 'Webhook'], ['schedule', 'Schedule'], ['event', 'Event']].forEach(([v, t]) => {
        const o = document.createElement('option'); o.value = v; o.textContent = t;
        if (v === (agent.trigger_type || 'user_input')) o.selected = true; trigSel.appendChild(o);
      });
      trig.ctrl.appendChild(trigSel);

      const keyRowObj = _cfgRow(list, 'Trigger Key', 'Identifier that fires this trigger.');
      const keyInput = document.createElement('input'); keyInput.type = 'text';
      keyInput.className = 'ac-input ac-input-sm'; keyInput.dataset.field = 'trigger_key';
      keyInput.value = agent.trigger_key || ''; keyInput.placeholder = _triggerKeyPlaceholder(agent);
      keyRowObj.ctrl.appendChild(keyInput);

      let confirmedTrig = agent.trigger_type || 'user_input';
      let confirmedKey = agent.trigger_key || '';
      trigSel.addEventListener('change', async () => {
        const selected = trigSel.value;
        if (selected === confirmedTrig) return;
        trigSel.disabled = true;
        const ok = await _saveCfg(agent, { trigger_type: selected }, trig.ctrl);
        trigSel.disabled = false;
        if (ok) { confirmedTrig = selected; }
        else { trigSel.value = confirmedTrig; }
      });
      const saveKey = _debounced(async () => {
        const val = keyInput.value;
        if (val === confirmedKey) return;
        const ok = await _saveCfg(agent, { trigger_key: val || null }, keyRowObj.ctrl);
        if (ok) confirmedKey = val;
      });
      keyInput.addEventListener('input', saveKey);
      keyInput.addEventListener('blur', () => saveKey.flush());
    }
  }

  // Member access belongs with the controls that govern who can reach this agent.
  // It is rendered by the shared identity renderer, then moved here intact so
  // its state, event handlers, and API behavior remain canonical.
  const memberAccessCard = identityTopic.querySelector('.agent-member-access');
  if (memberAccessCard) deployTopic.appendChild(memberAccessCard);

  // A draft has no agent id to share or embed. Keep Deploy & Share visible so its
  // place in the configuration remains clear, but do not build its live controls.
  if (isMock) {
    deployTopic.parentElement.classList.add('agent-config-topic--draft-locked');
    const notice = document.createElement('p');
    notice.className = 'agent-config-draft-lock-notice';
    notice.textContent = 'Deploy & Share becomes available after you create this agent.';
    deployTopic.appendChild(notice);
  } else {
    // ── Public anonymous policy ──────────────────────────────────────────────
    if (isEditable && agent.id !== 'shared_default') {
      _renderPublicAccessPolicy(deployTopic, agent);
    }

    // ── Website Embed (chat widget for external sites) ───────────────────────
    if (isEditable && agent.engine !== 'claude_code' && agent.engine !== 'terminal_chat') {
      _renderWebsiteEmbed(deployTopic, agent);
    }
  }

  // ── Template options (discoverable toggle) ──────────────────────────────────
  if (agent.source === 'template') {
    const g = _group(identityTopic, 'settings-2', 'Template options');
    const list = _cfgList(g);
    const { ctrl } = _cfgRow(list, 'Discoverable', 'Show this template in the "New Agent" creation dropdown.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = !!agent.discoverable; cb.dataset.field = 'discoverable';
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    if (!isMock) {
      cb.addEventListener('change', async () => {
        cb.disabled = true;
        const ok = await _saveCfg(agent, { discoverable: cb.checked }, ctrl);
        cb.disabled = false;
        if (!ok) cb.checked = !cb.checked;
      });
    }

  }

  // A protected/system agent may not expose every topic. Remove only truly
  // empty parents so the outline never contains a chevron that opens to nothing.
  topicHost.querySelectorAll(':scope > .agent-config-topic').forEach((topic) => {
    const content = topic.querySelector(':scope > .agent-config-topic-content');
    if (content && !content.children.length) topic.remove();
  });

  // ── Suggested replies (user-impersonator only) ──────────────────────────────
  function _renderSuggestionModeControl(body) {
    const g = _group(body, 'sparkles', 'Suggested replies');
    const intro = document.createElement('div'); intro.className = 'ac-hint';
    intro.textContent = 'When the user-impersonator agent responds, it can suggest reply chips the real user might tap.';
    g.appendChild(intro);
    const list = _cfgList(g);

    const modeRow = _cfgRow(list, 'When to suggest', 'Choose when the suggestion chips appear.');
    const modeSel = document.createElement('select');
    modeSel.className = 'ac-input ac-input-sm ac-config-sel';
    [['off', 'Off'], ['always', 'Always'], ['after_response', 'After each response']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (v === (agent.suggestion_mode || 'off')) o.selected = true; modeSel.appendChild(o);
    });
    modeRow.ctrl.appendChild(modeSel);

    const countRow = _cfgRow(list, 'How many suggestions', 'Number of chips to show.');
    const countInput = document.createElement('input'); countInput.type = 'number';
    countInput.className = 'ac-input ac-input-sm ac-config-num';
    countInput.min = 1; countInput.max = 8; countInput.step = 1;
    countInput.value = agent.suggestion_count || 3;
    countRow.ctrl.appendChild(_wrapNumberStepper(countInput));

    if (!isMock) {
      let confirmedMode = agent.suggestion_mode || 'off';
      let confirmedCount = agent.suggestion_count || 3;
      modeSel.addEventListener('change', async () => {
        const selected = modeSel.value;
        if (selected === confirmedMode) return;
        modeSel.disabled = true;
        const ok = await _saveCfg(agent, { suggestion_mode: selected }, modeRow.ctrl);
        modeSel.disabled = false;
        if (ok) { confirmedMode = selected; }
        else { modeSel.value = confirmedMode; }
      });
      const saveCount = _debounced(async () => {
        const val = parseInt(countInput.value, 10) || 3;
        if (val === confirmedCount) return;
        const ok = await _saveCfg(agent, { suggestion_count: val }, countRow.ctrl);
        if (ok) confirmedCount = val;
      });
      countInput.addEventListener('input', saveCount);
      countInput.addEventListener('blur', () => saveCount.flush());
    }
  }

  // Keep the advanced areas available without forcing every agent configuration
  // session to carry their dense controls. This is deliberately local UI state,
  // not an agent setting, so it never changes the agent for other users.
  const advancedToggle = document.createElement('label');
  advancedToggle.className = 'agent-config-advanced-toggle';
  const advancedCheckbox = document.createElement('input');
  advancedCheckbox.type = 'checkbox';
  advancedCheckbox.className = 'agent-config-advanced-checkbox';
  advancedCheckbox.checked = false;
  const advancedText = document.createElement('span');
  advancedText.className = 'agent-config-advanced-toggle-text';
  advancedText.textContent = 'Show Advanced';
  advancedToggle.append(advancedCheckbox, advancedText);
  advancedCheckbox.addEventListener('change', () => setAdvancedVisibility(advancedCheckbox.checked));
  landing.appendChild(advancedToggle);

  const savedConfigPage = _getAgentConfigPage(agent.id);
  const savedPageIsAvailable = savedConfigPage
    && (advancedCheckbox.checked || !ADVANCED_CONFIG_TOPIC_IDS.has(savedConfigPage));
  if (savedPageIsAvailable && !pageController.open(savedConfigPage)) {
    _setAgentConfigPage(agent.id, '');
  } else if (savedConfigPage && !savedPageIsAvailable) {
    _setAgentConfigPage(agent.id, '');
  }
}

/** Render one canonical Config topic page while preserving its existing controls. */
export function renderAgentConfigTopicPage(
  body,
  agent,
  panelEl,
  renderList,
  topicId,
  { onBackToDirectory = null } = {},
) {
  _setAgentConfigPage(agent.id, topicId);
  _renderConfigTab(body, agent, panelEl, renderList, {
    onPageChange: (pageId) => {
      if (!pageId) onBackToDirectory?.();
    },
  });
}

function _renderPublicAccessPolicy(body, agent) {
  const g = _group(body, 'shield', 'Public visitor policy');
  const intro = document.createElement('div'); intro.className = 'ac-hint';
  intro.textContent = 'Public agents must use the owner wallet or an agent-owned model key. Limits apply per agent; visitors keep a durable identity while saved chats expire separately.';
  g.appendChild(intro);
  const list = _cfgList(g);
  const policy = JSON.parse(JSON.stringify(agent.public_access || {}));
  policy.funding = policy.funding || {};
  policy.data = policy.data || {};
  policy.usage = policy.usage || {};
  policy.capabilities = policy.capabilities || {};
  policy.chat_ui = policy.chat_ui || {};

  const save = async (ctrl) => {
    policy.enabled = true;
    const ok = await _saveCfg(agent, { public_access: policy }, ctrl);
    if (ok) agent.public_access = JSON.parse(JSON.stringify(policy));
    return ok;
  };
  const addText = (label, hint, value, onValue, { type = 'text', placeholder = '' } = {}) => {
    const { ctrl } = _cfgRow(list, label, hint);
    const input = document.createElement('input'); input.type = type;
    input.className = 'ac-input ac-input-sm'; input.value = value ?? ''; input.placeholder = placeholder;
    ctrl.appendChild(input);
    input.addEventListener('change', async () => { onValue(input.value); await save(ctrl); });
    return input;
  };

  {
    const { ctrl } = _cfgRow(list, 'Token funding', 'Public requests fail closed if this funding source is unavailable. Dedicated key uses the key configured in this agent’s model settings.');
    const sel = document.createElement('select'); sel.className = 'ac-input ac-input-sm ac-config-sel';
    [['owner_wallet', 'Owner wallet'], ['dedicated_key', 'Agent-owned model key']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if ((policy.funding.mode || '') === v) o.selected = true;
      sel.appendChild(o);
    });
    ctrl.appendChild(sel);
    sel.addEventListener('change', async () => { policy.funding.mode = sel.value; await save(ctrl); });
  }

  addText('Turns / day', 'Maximum public turns across this agent each day.', policy.usage.turns_per_agent_per_day || 5000,
    v => { policy.usage.turns_per_agent_per_day = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Concurrent runs', 'Maximum simultaneous public runs for this agent.', policy.usage.concurrent_runs || 10,
    v => { policy.usage.concurrent_runs = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Guest tokens / day', 'Estimated admission tokens allowed per visitor per day.', policy.usage.tokens_per_guest_per_day || 100000,
    v => { policy.usage.tokens_per_guest_per_day = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Agent tokens / month', 'Estimated admission tokens sponsored across all visitors each month.', policy.usage.tokens_per_agent_per_month || 5000000,
    v => { policy.usage.tokens_per_agent_per_month = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Agent spend cap / month', 'Maximum estimated sponsored model cost in cents each month.', policy.usage.cost_cents_per_agent_per_month || 5000,
    v => { policy.usage.cost_cents_per_agent_per_month = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Chat retention (days)', 'Anonymous identity remains durable; only saved conversation data expires.', policy.data.session_retention_days || 14,
    v => { policy.data.session_retention_days = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Sessions / visitor', 'Maximum saved sessions for one anonymous visitor.', policy.data.max_sessions_per_guest || 5,
    v => { policy.data.max_sessions_per_guest = Math.max(1, Number(v) || 1); }, { type: 'number' });
  addText('Transcript MiB / visitor', 'Maximum saved transcript content for one visitor.', Math.round((policy.data.max_transcript_bytes_per_guest || 1048576) / 1048576),
    v => { policy.data.max_transcript_bytes_per_guest = Math.max(1, Number(v) || 1) * 1048576; }, { type: 'number' });
  addText('Total transcript MiB', 'Maximum anonymous transcript content stored for this agent.', Math.round((policy.data.max_total_storage_bytes || 1073741824) / 1048576),
    v => { policy.data.max_total_storage_bytes = Math.max(1, Number(v) || 1) * 1048576; }, { type: 'number' });
  addText('Public abilities', 'Comma-separated ability IDs. Wildcards and platform-sensitive groups are rejected.', (policy.capabilities.abilities || []).join(', '),
    v => { policy.capabilities.abilities = v.split(',').map(x => x.trim()).filter(Boolean); }, { placeholder: 'web_search, company_lookup' });
  addText('Public tools', 'Comma-separated tool names exposed to anonymous visitors.', (policy.capabilities.tools || []).join(', '),
    v => { policy.capabilities.tools = v.split(',').map(x => x.trim()).filter(Boolean); }, { placeholder: 'lookup_order, search_docs' });
  addText('UI feature grants', 'Comma-separated feature gates, such as model_picker. Features do not grant tools or abilities.', (policy.capabilities.features || []).join(', '),
    v => { policy.capabilities.features = v.split(',').map(x => x.trim()).filter(Boolean); }, { placeholder: 'model_picker' });

  const uiControls = [
    ['File attachment button', 'attach', ['active_footer', 'idle_footer']],
    ['Model picker button', 'model_changer', ['active_footer']],
    ['Abilities button', 'abilities', ['active_footer']],
  ];
  uiControls.forEach(([label, control, surfaces]) => {
    let enabled = true;
    const common = policy.chat_ui.chat_common || {};
    for (const surface of surfaces) {
      const configured = common[surface]?.chat_pill?.controls?.[control]?.enabled
        ?? common[surface]?.below_pill?.controls?.[control]?.enabled;
      if (configured === false) enabled = false;
    }
    const { ctrl } = _cfgRow(list, label, 'Show this control to public visitors.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle'; cb.checked = enabled;
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    cb.addEventListener('change', async () => {
      policy.chat_ui.chat_common = policy.chat_ui.chat_common || {};
      surfaces.forEach(surface => {
        const parent = control === 'attach' ? 'chat_pill' : 'below_pill';
        policy.chat_ui.chat_common[surface] = policy.chat_ui.chat_common[surface] || {};
        policy.chat_ui.chat_common[surface][parent] = policy.chat_ui.chat_common[surface][parent] || {};
        const slot = policy.chat_ui.chat_common[surface][parent];
        slot.controls = slot.controls || {};
        slot.controls[control] = { ...(slot.controls[control] || {}), enabled: cb.checked };
      });
      await save(ctrl);
    });
  });
}

// ── Website Embed section ───────────────────────────────────────────────────────
function _renderWebsiteEmbed(body, agent) {
  const g = _group(body, 'globe', 'Website Embed');
  const emb = (agent.embed && typeof agent.embed === 'object') ? agent.embed : {};

  const intro = document.createElement('div'); intro.className = 'ac-hint';
  intro.innerHTML = 'Embed this agent as a chat widget on any website. Visitors chat without signing up — '
    + 'this requires <strong>User&nbsp;Mode&nbsp;=&nbsp;Anonymous</strong> (set it under Deploy&nbsp;&amp;&nbsp;Share). '
    + 'The widget\'s appearance (accent, messages, launcher style) is set in the agent\'s chat UI config.';
  g.appendChild(intro);

  const list = _cfgList(g);

  // Enable toggle
  const anonWarn = document.createElement('div');
  anonWarn.className = 'ac-hint';
  anonWarn.style.color = 'var(--warn, #d97706)';
  const refreshAnonWarn = () => {
    const on = !!(agent.embed && agent.embed.enabled);
    const anon = (agent.user_mode || 'anonymous') === 'anonymous';
    anonWarn.hidden = !(on && !anon);
    anonWarn.textContent = 'The widget is enabled but User Mode isn\'t Anonymous, so visitors will be turned away. '
      + 'Set User Mode to Anonymous above.';
  };
  {
    const { ctrl } = _cfgRow(list, 'Enable widget', 'Turn the embeddable website widget on for this agent.');
    const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle';
    cb.checked = !!emb.enabled;
    const track = document.createElement('span'); track.className = 'conn-toggle-track';
    wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
    cb.addEventListener('change', async () => {
      cb.disabled = true;
      const ok = await _saveCfg(agent, { embed: { enabled: cb.checked } }, ctrl);
      cb.disabled = false;
      if (!ok) cb.checked = !cb.checked;
      refreshAnonWarn();
      refreshSnippet();
    });
  }
  g.appendChild(anonWarn);

  // Launcher position
  {
    const { ctrl } = _cfgRow(list, 'Launcher position', 'Which corner the chat bubble sits in.');
    const sel = document.createElement('select'); sel.className = 'ac-input ac-input-sm ac-config-sel';
    [['right', 'Bottom right'], ['left', 'Bottom left']].forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if ((emb.launcher_position || 'right') === v) o.selected = true; sel.appendChild(o);
    });
    ctrl.appendChild(sel);
    sel.addEventListener('change', () => _saveCfg(agent, { embed: { launcher_position: sel.value } }, ctrl));
  }

  // Allowed domains
  _cfgField(g, {
    label: 'Allowed domains',
    field: 'embed_allowed_domains',
    value: Array.isArray(emb.allowed_domains) ? emb.allowed_domains.join(', ') : '',
    multiline: true, rows: 2,
    placeholder: 'example.com, app.example.com  (blank = allow any site)',
    hint: 'Restrict which sites may embed this widget. Leave blank to allow embedding anywhere.',
    onSave: (val) => _putAgentField(agent, { embed: { allowed_domains: val } }, null, { silent: true }),
  });

  // Snippet + preview
  const snipWrap = document.createElement('div'); snipWrap.className = 'ac-cfg-field';
  const snipLbl = document.createElement('label'); snipLbl.className = 'ac-label'; snipLbl.textContent = 'Embed snippet';
  snipWrap.appendChild(snipLbl);
  const snip = document.createElement('textarea');
  snip.className = 'ac-input'; snip.rows = 2; snip.readOnly = true;
  snip.style.fontFamily = 'var(--font-mono, monospace)'; snip.style.fontSize = '12.5px';
  snipWrap.appendChild(snip);
  const snipHint = document.createElement('div'); snipHint.className = 'ac-hint';
  snipHint.textContent = 'Paste this before </body> on your site.';
  snipWrap.appendChild(snipHint);
  const actions = document.createElement('div'); actions.style.display = 'flex'; actions.style.gap = '8px'; actions.style.marginTop = '8px';
  const copyBtn = _btn('Copy snippet', 'agents-btn'); actions.appendChild(copyBtn);
  const previewBtn = _btn('Preview widget', 'agents-btn'); actions.appendChild(previewBtn);
  snipWrap.appendChild(actions);
  g.appendChild(snipWrap);

  copyBtn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(snip.value); copyBtn.textContent = 'Copied ✓'; setTimeout(() => copyBtn.textContent = 'Copy snippet', 1500); }
    catch (_) { snip.select(); document.execCommand && document.execCommand('copy'); }
  });
  previewBtn.addEventListener('click', () => window.open(`/embed/${agent.id}`, '_blank', 'noopener'));

  function refreshSnippet() {
    fetch(`/api/v1/agents/${agent.id}/embed`).then(r => r.ok ? r.json() : null).then(info => {
      if (!info) return;
      snip.value = info.snippet || '';
      const off = !info.embeddable;
      snipWrap.style.opacity = off ? '0.55' : '1';
    }).catch(() => {});
  }
  refreshAnonWarn();
  refreshSnippet();
}

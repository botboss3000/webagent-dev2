'use strict';

// Trusted renderer for declarative chat components.  The agent supplies data only;
// this module owns all DOM creation and action routing.
// CHAT-COMPONENT-PANEL / CHAT-COMPONENT-FULL-VIEW: every component renders into
// the single agent panel (#chat-component-panel / -body in chat-side-panel.html).
// The FULL view is the panel's ONLY open state — a full-height overlay pinned
// below the chat header tabs and above the composer. The old drawer (tucked
// behind the pill) and the ⛶ expand toggle were retired.
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon, componentTypeIcon } from '../../shared/js/icons.js';
import { watchComponents, reconcileComponents } from '../core/components.js';

let host;
let panel;
let components = new Map();
let panelUserClosed = false;   // user collapsed the panel; live content stays silent unless configuration opts in
let activeComponentId = null;  // which component the panel displays (tabs switch it)
let componentSession = null, componentUser = null, watcher = null, restoring = false, batching = false;

function _autoOpenOnPresent() {
  const ui = window.__chatUiSnapshot || {};
  const chatPanel = document.getElementById('chat-panel');
  const isNarrow = chatPanel ? chatPanel.clientWidth <= 768 : window.innerWidth <= 768;
  const surface = window.__CHAT_PORTAL__
    ? ui.chat_widget
    : (isNarrow ? ui.chat_mobile : ui.chat_desktop);
  const surfaceValue = surface?.component_panel?.auto_open_on_present;
  if (surfaceValue !== undefined) return !!surfaceValue;
  return !!ui.chat_common?.component_panel?.auto_open_on_present;
}

function _host() {
  if (host && host.isConnected) return host;
  host = document.getElementById('chat-component-panel-body');
  return host;
}

function _panel() {
  if (panel && panel.isConnected) return panel;
  panel = document.getElementById('chat-component-panel');
  return panel;
}

// Open the panel whenever it holds content and the user hasn't collapsed it.
// Called after every render/remove.
// States: empty → fully hidden; has content but user-collapsed → panel hidden;
// open → full-height overlay showing ONLY the active component (tabs switch
// which one). New agent content reopens and selects the freshly presented one.
function _syncPanel() {
  const p = _panel();
  const h = _host();
  if (!p || !h) return;
  const ids = [...components.keys()];
  const has = ids.length > 0;
  // Resolve selection: keep the current pick if it still exists, else first.
  if (has && !ids.includes(activeComponentId)) activeComponentId = ids[0];
  if (!has) activeComponentId = null;
  const open = has && !panelUserClosed;
  // CHAT-COMPONENT-FULL-VIEW: the full-height overlay is the panel's ONLY open
  // state. The panel itself is fixed below the header tabs and above the
  // composer; _positionFullView measures its transcript-aligned width.
  p.classList.toggle('has-components', has);
  p.classList.toggle('open', open);
  if (open) {
    _positionFullView();
  } else {
    // Collapsed: drop fixed-view geometry so the panel has no visible footprint
    // in the composer column.
    p.style.top = ''; p.style.left = ''; p.style.right = '';
    p.style.bottom = ''; p.style.width = ''; p.style.height = ''; p.style.maxWidth = '';
  }
  p.setAttribute('aria-hidden', has ? 'false' : 'true');
  // Show only the active card; keep the others mounted (state preserved).
  for (const card of h.querySelectorAll('[data-chat-component-id]')) {
    card.classList.toggle('cc-hidden', card.dataset.chatComponentId !== activeComponentId);
  }
  // Head title reflects the active component: its title, plus a done/total
  // counter for todo lists ("Plan Mode Checklist 5/6"). The type icon follows
  // the COMPONENT-TYPE-ICON-STANDARD (check-square / square-menu / square-square).
  const titleEl = document.getElementById('chat-component-panel-title');
  const typeIconEl = document.getElementById('chat-component-panel-type-icon');
  if (titleEl) {
    const active = activeComponentId ? components.get(activeComponentId) : null;
    if (active) {
      let label = active.title || active.type || 'Agent panel';
      if (active.type === 'todo_list') {
        const items = (active.data && active.data.items) || [];
        const done = items.filter(i => i.done).length;
        label += ` ${done}/${items.length}`;
      }
      titleEl.textContent = label;
    } else {
      titleEl.textContent = 'Agent panel';
    }
  }
  if (typeIconEl) {
    const active = activeComponentId ? components.get(activeComponentId) : null;
    typeIconEl.innerHTML = active ? icon(componentTypeIcon(active.type), { size: '13px' }) : '';
  }
}

// CHAT-COMPONENT-FULL-VIEW: copy the transcript's rendered horizontal bounds.
// The transcript already resolves chat_common.content_max_width through CSS,
// including unit-bearing values such as `100%` on mobile and `1000px` on
// desktop. Do not parse that token again here: parseFloat('100%') is 100 and
// used to collapse the mobile panel to 100px, leaving large side clearances.
export function _fullViewHorizontalBounds(chatPanel, inner, gap = 4) {
  const ir = inner?.getBoundingClientRect();
  if (ir && ir.width > 0) return { left: ir.left, width: ir.width };
  const pr = chatPanel.getBoundingClientRect();
  return {
    left: pr.left + gap,
    width: Math.max(0, pr.width - gap * 2),
  };
}

function _positionFullView() {
  const p = _panel();
  const chatPanel = document.getElementById('chat-panel');
  if (!p || !chatPanel || !p.classList.contains('open')) return;
  const pr = chatPanel.getBoundingClientRect();
  const gap = 4;
  let top = pr.top;
  const header = document.getElementById('chat-header');
  if (header) top = Math.max(top, header.getBoundingClientRect().bottom);
  const sub = document.getElementById('chat-sub-header');
  if (sub && sub.getBoundingClientRect().height > 0) {
    top = Math.max(top, sub.getBoundingClientRect().bottom);
  }
  // The element is deliberately at most 75% of the CHAT PANEL (not the
  // viewport) and preserves the original header-relative placement. Its lower
  // edge ends flush with the floating composer, so only the height—not the
  // approved top edge—is capped at the composer top.
  const panelTop = top + gap;
  const panelBottom = pr.bottom - gap;
  const composer = document.getElementById('chat-input-area');
  const safeBottom = composer
    ? Math.min(panelBottom, composer.getBoundingClientRect().top)
    : panelBottom;
  const availableHeight = Math.max(0, safeBottom - panelTop);
  const height = Math.min(Math.max(0, pr.height * 0.75), availableHeight);
  // The transcript owns all responsive width resolution. Copy its rendered
  // box instead of interpreting CSS units in JavaScript; this works for both
  // mobile `100%` and desktop pixel caps and follows future layout changes.
  const inner = document.getElementById('chat-messages-inner');
  const { width, left } = _fullViewHorizontalBounds(chatPanel, inner, gap);
  // Preserve the original chat-element placement: start just below the visible
  // header stack. Height is explicit, so leave bottom automatic rather than
  // over-constraining the fixed box and losing the 75% target.
  p.style.top = panelTop + 'px';
  p.style.left = left + 'px';
  p.style.width = width + 'px';
  p.style.right = 'auto';
  p.style.bottom = 'auto';
  p.style.height = height + 'px';
  p.style.maxWidth = 'none';
}

// A component is "done" once its purpose is fulfilled — all todo items ticked,
// a choice selected, a form submitted, an approval resolved. Completed
// components leave the panel immediately and their header tab disappears.
function _isCompleted(component) {
  const data = component.data || {};
  if (component.type === 'todo_list') {
    const items = data.items || [];
    return items.length > 0 && items.every(i => i.done);
  }
  if (component.type === 'choice') return !!data.selected;
  if (component.type === 'form') return !!data.submitted;
  if (component.type === 'approval') return !!data.status;
  return false;
}

// Broadcast that the active component set changed (added / completed /
// deleted). The sub-agent tab bar listens and re-fetches so component tabs
// appear and clear live.
function _notifyChanged() {
  if(batching) return;
  // PREVENTIVE-COMMENTS: `bubbles: true` is REQUIRED, not decorative. The chip
  // strip listens on `window` (session-dropdown/list.js), and a NON-bubbling
  // event dispatched on `document` never reaches a window listener. Without it
  // the live component-tab refresh silently never fires and a new tab only
  // appears once the 15s chip cache lapses.
  document.dispatchEvent(new CustomEvent('chat-components-changed', {
    bubbles: true,
    detail: { sessionId: componentSession, components: [...components.values()] },
  }));
}

// ── Panel layout state persistence ─────────────────────────────────────────
// The panel's per-session UI state — collapsed flag + active component tab — is
// saved to the server so reopening the session restores which document was
// showing. The full view has no drawer geometry to persist. Debounced so a burst
// of opens/closes coalesces into one PUT.
let _uiSaveTimer = null;
let _applyUiState = null;   // assigned inside initChatComponents (needs drag clamps)

function _uiState() {
  // CHAT-COMPONENT-FULL-VIEW: the full view is the panel's only open state, so
  // there is no drawer geometry to persist — just whether it is collapsed and
  // which component tab is active.
  return {
    minimized: panelUserClosed,
    active: activeComponentId || '',
  };
}

function _scheduleUiSave() {
  if (_uiSaveTimer) clearTimeout(_uiSaveTimer);
  _uiSaveTimer = setTimeout(() => { _uiSaveTimer = null; _saveUiState(); }, 400);
}

async function _saveUiState() {
  const sid = componentSession;
  if (!sid || sid !== app.currentSessionId || componentUser !== app.currentUserId
      || !components.size) return;
  try {
    await fetch(apiPath('/api/v1/chat/components/ui-state?session_id=' + encodeURIComponent(sid)), {
      method: 'PUT', keepalive: true,
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ ui: _uiState() }),
    });
  } catch (_) { /* panel layout persistence is best-effort */ }
}

function _flushUiSave() {
  if (_uiSaveTimer) { clearTimeout(_uiSaveTimer); _uiSaveTimer = null; _saveUiState(); }
}

// ── Renderers per type ──────────────────────────────────────────────────────

function _todo(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-todo';
  const list = document.createElement('div'); list.className = 'chat-component-list';
  for (const item of component.data.items || []) {
    const label = document.createElement('label'); label.className = 'chat-component-item';
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = !!item.done;
    input.addEventListener('change', async () => {
      input.disabled = true;
      try { await act(component.id, 'toggle_item', { item_id: item.id, done: input.checked }); }
      catch (_) { input.checked = !input.checked; }
      input.disabled = false;
    });
    const text = document.createElement('span'); text.textContent = item.label;
    label.append(input, text); list.appendChild(label);
  }
  card.appendChild(list); return card;
}

function _status(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-status';
  // Render as markdown (the plan overview is a markdown document, not a grid).
  const data = component.data || {};
  let md = data.markdown;
  if (!md && Array.isArray(data.sections)) {
    md = data.sections.map(s =>
      `**${s.label}**\n\n${s.status ? `*${s.status}* — ` : ''}${s.value || ''}`
    ).join('\n\n');
  }
  if (md && typeof app._renderMarkdownBody === 'function') {
    const body = app._renderMarkdownBody(md, false);
    if (body) { card.appendChild(body); return card; }
  }
  // Fallback: plain text
  const p = document.createElement('p');
  p.textContent = (md || '').replace(/[*#`>_-]/g, '') || component.data.message || '';
  card.appendChild(p);
  return card;
}

function _choice(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-choice';
  const selected = component.data.selected || '';
  for (const opt of component.data.options || []) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-component-choice-btn';
    if (opt.id === selected) btn.classList.add('active');
    btn.textContent = opt.label;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await act(component.id, 'select', { option_id: opt.id }); }
      catch (_) { btn.disabled = false; }
    });
    card.appendChild(btn);
  }
  return card;
}

function _form(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-form';
  const submitted = component.data.submitted;
  if (submitted) {
    const done = document.createElement('p');
    done.className = 'chat-component-form-done';
    done.textContent = '✓ Submitted';
    card.appendChild(done);
    return card;
  }
  const fields = component.data.fields || [];
  const fieldEls = {};
  for (const f of fields) {
    const wrap = document.createElement('div'); wrap.className = 'chat-component-form-field';
    const lbl = document.createElement('label'); lbl.textContent = f.label;
    let input;
    if (f.type === 'select' && f.options) {
      input = document.createElement('select');
      for (const o of f.options) {
        const opt = document.createElement('option'); opt.value = o.value; opt.textContent = o.label;
        input.appendChild(opt);
      }
    } else if (f.multiline) {
      input = document.createElement('textarea'); input.rows = 3;
    } else {
      input = document.createElement('input'); input.type = f.type || 'text';
    }
    if (f.required) input.required = true;
    input.name = f.name;
    wrap.append(lbl, input); card.appendChild(wrap);
    fieldEls[f.name] = input;
  }
  const submit = document.createElement('button');
  submit.type = 'button'; submit.className = 'chat-component-form-submit'; submit.textContent = 'Submit';
  submit.addEventListener('click', async () => {
    const values = {};
    for (const f of fields) {
      const el = fieldEls[f.name];
      if (!el) continue;
      if (!el.reportValidity()) return;
      values[f.name] = el.value;
    }
    submit.disabled = true;
    try { await act(component.id, 'submit', { values }); }
    catch (_) { submit.disabled = false; }
  });
  card.appendChild(submit);
  return card;
}

function _approval(component) {
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-approval';
  const status = component.data.status || '';
  if (status) {
    const done = document.createElement('p');
    done.className = 'chat-component-approval-result';
    done.textContent = status === 'approved' ? '✓ Approved' : '✗ Rejected';
    if (status === 'approved') done.classList.add('approved');
    else done.classList.add('rejected');
    card.appendChild(done);
    return card;
  }
  if (component.data.description) {
    const desc = document.createElement('p'); desc.textContent = component.data.description;
    card.appendChild(desc);
  }
  const actions = document.createElement('div'); actions.className = 'chat-component-approval-actions';
  const approve = document.createElement('button');
  approve.type = 'button'; approve.className = 'chat-component-approval-approve';
  approve.textContent = component.data.approve_label || 'Approve';
  approve.addEventListener('click', async () => {
    approve.disabled = true; reject.disabled = true;
    try { await act(component.id, 'approve', {}); }
    catch (_) { approve.disabled = false; reject.disabled = false; }
  });
  const reject = document.createElement('button');
  reject.type = 'button'; reject.className = 'chat-component-approval-reject';
  reject.textContent = component.data.reject_label || 'Reject';
  reject.addEventListener('click', async () => {
    approve.disabled = true; reject.disabled = true;
    try { await act(component.id, 'reject', {}); }
    catch (_) { approve.disabled = false; reject.disabled = false; }
  });
  actions.append(approve, reject); card.appendChild(actions);
  return card;
}

// SCRATCHPAD-ROWS: the user's Scratchpad document. Unlike every other component
// type the USER owns this content — they add, edit and delete the rows
// themselves. Each row carries an explicit "Share with agent" toggle: shared
// rows are injected into the agent's context each turn, unshared rows never
// leave the browser. Actions run through one promise chain so two quick edits
// can never race each other into a lost update.
let _scratchpadChain = Promise.resolve();
function _scratchpadAct(id, action, payload) {
  const sid=componentSession, user=componentUser, activeWatcher=watcher;
  const run = () => {
    if(sid!==app.currentSessionId || user!==app.currentUserId || watcher!==activeWatcher)
      throw new Error('The active session changed before the edit was saved');
    return act(id, action, payload);
  };
  const next = _scratchpadChain.then(run, run);
  _scratchpadChain = next.then(() => {}, () => {});
  return next;
}

function _scratchpad(component) {
  const rows = (component.data && component.data.rows) || [];
  const card = document.createElement('section');
  card.className = 'chat-component chat-component-scratchpad';
  const list = document.createElement('div'); list.className = 'chat-component-scratchpad-list';
  const commit = (el, action, payload, undo) => {
    el.disabled = true;
    _scratchpadAct(component.id, action, payload)
      .catch(() => { if (undo) undo(); })
      .finally(() => { if (el.isConnected) el.disabled = false; });
  };
  for (const row of rows) {
    const wrap = document.createElement('div'); wrap.className = 'chat-component-scratchpad-row';
    const text = document.createElement('textarea');
    text.className = 'chat-component-scratchpad-text';
    text.rows = 2; text.value = row.text || ''; text.placeholder = 'Add a note\u2026';
    // Blur commits — a change event never fires mid-keystroke, so one edit
    // produces exactly one write. \u2318/Ctrl+Enter commits without leaving.
    text.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); text.blur(); }
    });
    text.addEventListener('change', () => {
      if ((text.value || '') === (row.text || '')) return;
      const prev = text.value;
      commit(text, 'update_row', { row_id: row.id, text: text.value }, () => { text.value = prev; });
    });
    const controls = document.createElement('div'); controls.className = 'chat-component-scratchpad-controls';
    const share = document.createElement('label');
    share.className = 'chat-component-scratchpad-share';
    if (row.shared) share.classList.add('is-shared');
    const box = document.createElement('input'); box.type = 'checkbox'; box.checked = !!row.shared;
    box.addEventListener('change', () => {
      commit(box, 'toggle_share', { row_id: row.id, shared: box.checked },
             () => { box.checked = !box.checked; });
    });
    const shareText = document.createElement('span'); shareText.textContent = 'Share with agent';
    share.append(box, shareText);
    const del = document.createElement('button');
    del.type = 'button'; del.className = 'chat-component-scratchpad-del';
    del.title = 'Delete row'; del.setAttribute('aria-label', 'Delete row');
    del.innerHTML = icon('x', { size: '13px' });
    del.addEventListener('click', () => commit(del, 'delete_row', { row_id: row.id }));
    controls.append(share, del);
    wrap.append(text, controls); list.appendChild(wrap);
  }
  card.appendChild(list);
  const add = document.createElement('button');
  add.type = 'button'; add.className = 'chat-component-scratchpad-add'; add.textContent = 'Add row';
  add.addEventListener('click', () => {
    add.disabled = true;
    _scratchpadAct(component.id, 'add_row', {})
      .then(() => {
        const boxes = _host() && _host().querySelectorAll('.chat-component-scratchpad-text');
        const last = boxes && boxes[boxes.length - 1];
        if (last) last.focus();
      })
      .catch(() => {})
      .finally(() => { if (add.isConnected) add.disabled = false; });
  });
  card.appendChild(add);
  return card;
}

const RENDERERS = {
  todo_list: _todo,
  status:    _status,
  choice:    _choice,
  form:      _form,
  approval:  _approval,
  scratchpad: _scratchpad,
};

// ── Core render / persist / act / remove ────────────────────────────────────

function render(component, target) {
  if (!component || !component.id) return null;
  if (_isCompleted(component)) { removeLocal(component.id); _notifyChanged(); return null; }
  const existing = document.querySelector(`[data-chat-component-id="${CSS.escape(component.id)}"]`);
  if (existing) existing.remove();
  const renderer = RENDERERS[component.type];
  const el = renderer ? renderer(component) : document.createElement('section');
  if (!renderer) {
    el.className = 'chat-component';
    const title = document.createElement('h3'); title.textContent = component.title;
    const text = document.createElement('p'); text.textContent = String(component.data.message || '');
    el.append(title, text);
  }
  el.dataset.chatComponentId = component.id;
  _host()?.appendChild(el);
  components.set(component.id, component);
  if (!activeComponentId) activeComponentId = component.id;  // first card becomes active
  _syncPanel();
  _notifyChanged();
  return el;
}

// Remove a component card from the panel (no server call).
function removeLocal(id) {
  const existing = document.querySelector(`[data-chat-component-id="${CSS.escape(id)}"]`);
  if (existing) existing.remove();
  components.delete(id);
  if (activeComponentId === id) activeComponentId = null;  // _syncPanel picks the next
  _syncPanel();
}

async function persist(component) {
  if (!app.currentSessionId) return null;
  const response = await fetch(apiPath('/api/v1/chat/components?session_id=' + encodeURIComponent(app.currentSessionId)), {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ component }),
  });
  if (!response.ok) throw new Error('Could not save component');
  return (await response.json()).component;
}

async function act(id, action, payload) {
  const sid=componentSession, user=componentUser, activeWatcher=watcher;
  if(!sid || sid!==app.currentSessionId || user!==app.currentUserId)
    throw new Error('The active session changed before the action');
  const response = await fetch(apiPath('/api/v1/chat/components/' + encodeURIComponent(id) + '/actions?session_id=' + encodeURIComponent(sid)), {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ action, payload }),
  });
  if (!response.ok) throw new Error('Could not update component');
  const component = (await response.json()).component;
  if(sid===app.currentSessionId && user===app.currentUserId && watcher===activeWatcher) {
    render(component);
    // Invalidate any older in-flight snapshot before it can undo this action.
    void watcher?.refresh();
  }
  return component;
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

function clear() {
  if (_uiSaveTimer) { clearTimeout(_uiSaveTimer); _uiSaveTimer=null; }
  components = new Map(); panelUserClosed = true; activeComponentId = null;
  const body = _host(); if (body) body.replaceChildren();
  _syncPanel();
  _notifyChanged();
}

function syncSnapshot(snapshot) {
  if(snapshot.session_id!==componentSession) throw new Error('Foreign elements snapshot');
  const rows=snapshot.components.filter(row=>!row.deleted && !_isCompleted(row));
  const fresh=rows.some(row=>!components.has(row.id));
  const before=JSON.stringify([...components.values()]);
  batching=true;
  try {
    reconcileComponents(components,rows,{
      render,
      remove:removeLocal,
      focused:id=>{
        const card=_host()?.querySelector('[data-chat-component-id="'+CSS.escape(id)+'"]');
        return !!card?.contains(document.activeElement);
      },
    });
  } finally { batching=false; }
  if(restoring) {
    _applyUiState?.(snapshot.ui || {});
    restoring=false;
  } else if(fresh && _autoOpenOnPresent()) {
    panelUserClosed=false;
  }
  _syncPanel();
  // The menu receives the same committed snapshot as the panel.
  if(before!==JSON.stringify([...components.values()])) _notifyChanged();
}

async function load(sessionId) {
  if(sessionId && sessionId===componentSession && componentUser===app.currentUserId && watcher)
    return watcher.refresh();
  watcher?.stop();watcher=null;
  componentSession=sessionId;componentUser=app.currentUserId;restoring=true;
  clear();
  if(!sessionId || sessionId!==app.currentSessionId || !componentUser) return;
  const user=componentUser;
  watcher=watchComponents({
    current:()=>sessionId===app.currentSessionId && user===app.currentUserId,
    read:signal=>fetch(apiPath('/api/v2/chat/sessions/'+encodeURIComponent(sessionId)+'/components'),
      {headers:authHeaders(),cache:'no-store',signal}),
    apply:syncSnapshot,
    clear,
    onError:message=>{
      const body=_host();if(!body) return;
      let status=body.querySelector('[data-component-sync-status]');
      if(!message) {status?.remove();return;}
      if(!status) {
        status=document.createElement('p');status.dataset.componentSyncStatus='';
        status.setAttribute('role','status');body.appendChild(status);
      }
      status.textContent=message;
    },
  });
  return watcher.refresh();
}

async function present(component) {
  // The present tool already persisted this component server-side (durable
  // even if this tab missed the live event); the streamed result only drives
  // the render. Reloads hydrate via load() → GET /chat/components.
  // CHAT-COMPONENT-SILENT-PRESENT: components are background session resources
  // by default. Header tabs are the explicit opening control; an individual
  // chat surface can opt into live opening through chat_ui.json.
  const hadComponents = components.size > 0;
  if (_autoOpenOnPresent()) {
    panelUserClosed = false;
    activeComponentId = component.id;
  } else if (!hadComponents) {
    panelUserClosed = true;
  }
  render(component);
  _scheduleUiSave();
}

// Compat no-op: components used to attach to the next agent bubble; they now
// render into the agent panel immediately (CHAT-COMPONENT-PANEL).
function attachPendingInline() { /* intentionally empty */ }

export function initChatComponents() {
  // Public controls for the tab bar: show = select a component (or keep the
  // current one) and open the full view; hide = close it. toggle =
  // open that component, or close if it's the one already showing.
  // No-ops when the panel has no content.
  app.componentPanelShow = (id) => {
    if (id && components.has(id)) activeComponentId = id;
    panelUserClosed = false; _syncPanel(); _scheduleUiSave();
  };
  app.componentPanelHide = () => { panelUserClosed = true; _syncPanel(); _scheduleUiSave(); };
  app.componentPanelToggle = (id) => {
    const p = _panel();
    if (!p || !p.classList.contains('has-components')) return;
    if (p.classList.contains('open') && activeComponentId === id) app.componentPanelHide();
    else app.componentPanelShow(id);
  };
  // Close button (—) in the panel header — THE hide control. The full view is
  // the panel's only open state, so closing simply collapses it.
  const minBtn = document.getElementById('chat-component-panel-min');
  if (minBtn && !minBtn.dataset.wired) {
    minBtn.dataset.wired = '1';
    minBtn.addEventListener('click', () => {
      panelUserClosed = true;
      _syncPanel();
      _scheduleUiSave();
    });
  }

  // Keep the overlay pinned as the surrounding chrome changes: window resize,
  // the header tabs resizing (chips appearing / age labels widening), and the
  // composer growing as the user types. All are no-ops while the panel is shut.
  const _reposition = () => {
    const p = _panel();
    if (p && p.classList.contains('open')) _positionFullView();
  };
  window.addEventListener('resize', _reposition);
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(_reposition);
    for (const el of ['chat-header', 'chat-sub-header', 'chat-input-area']) {
      const node = document.getElementById(el);
      if (node) ro.observe(node);
    }
  }

  // Restore the saved active tab after components mount (called from load()).
  // The full view has no saved geometry to restore (no drawer height/width);
  // components reopen as background session resources with the panel closed.
  _applyUiState = (ui) => {
    if (!ui || !components.size) return;
    if (typeof ui.active === 'string' && ui.active && components.has(ui.active)) {
      activeComponentId = ui.active;
    }
    // CHAT-COMPONENT-SILENT-RESTORE: components reopen as background session
    // resources; the full view starts closed. Header tabs are the explicit
    // entry point for every document type.
    panelUserClosed = true;
    _syncPanel();
  };

  // Flush any pending layout save when the tab closes/hides, so the last
  // minimize/resize survives a quick tab switch.
  window.addEventListener('pagehide', _flushUiSave);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') _flushUiSave();
  });

  app.loadChatComponents = load; app.presentChatComponent = present; app.attachPendingChatComponents = attachPendingInline;
  app.ensureChatComponents = sid => {
    if(sid!==componentSession || componentUser!==app.currentUserId) void load(sid);
  };
  document.addEventListener('webagent-auth-transition-start',()=>load(null));
}

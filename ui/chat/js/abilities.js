'use strict';

// Abilities status panel in the chat footer + counter badge.
//
// The footer Abilities panel (reuses the old Skills picker's button + look).
// A footer button (#chat-abilities-btn) carries a counter badge = the number of
// ACTIVE abilities, and opens a floating panel listing ALL the agent's enabled
// abilities. An ability is "active" (normal colour + highlighted row) when it is
// `visible` (its tools + skill are shown to the agent now) OR the agent pulled it
// in via load_ability — and the user hasn't turned it off here. Idle abilities
// render dull + unhighlighted.
//
// Clickable: clicking a row toggles the ability ON/OFF for THIS conversation.
// Activating arms it (a discoverable ability is loaded like load_ability);
// deactivating withholds its tools + skill on the next turn. The agent can still
// engage a discoverable ability itself via load_ability — its loads show up here
// after the reply finalizes.
//
// Refreshed on session switch and after each agent reply finalizes.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _sessionsCache } from "./session-list.js";

// ── State ──
let _inFlight = false;
let _cached = [];         // [{ id, display_name, icon, mode, loaded, active }]

let _missingSessionId = null;
let _pickerEl = null;     // floating .chat-ability-picker container
let _pickerList = null;   // .csp-list inside the picker
let _badgeEl = null;      // #chat-abilities-badge
let _btnEl = null;        // #chat-abilities-btn

function _agentId() {
  // Prefer the reactive current agent (the session's real agent) over the
  // Agents-tab/genui `selectedAgentId` localStorage value, which can hold a
  // stale or deleted agent id (e.g. a mock-agent creation that never landed)
  // and would 404 the abilities fetch. Mirrors chat-ui.js's agent resolution.
  try { return app.currentAgentId || localStorage.getItem('selectedAgentId') || ''; } catch (_) { return ''; }
}

// ── Badge (counter on the footer button) ──
function _updateBadge(count) {
  if (!_badgeEl) return;
  const n = Number(count) || 0;
  _badgeEl.textContent = n;
  _badgeEl.classList.toggle('visible', n > 0);
}

// ── Panel (floating, right-aligned) — reuses the skill-picker styling ──
function _buildPicker() {
  if (_pickerEl) return;
  const picker = document.createElement('div');
  picker.className = 'chat-skill-picker chat-ability-picker';
  // Seed the inline display so the very first _togglePicker() sees 'none' and
  // OPENS instead of toggling closed (the CSS default of display:none isn't
  // reflected on style.display, which starts as '').
  picker.style.display = 'none';

  const header = document.createElement('div');
  header.className = 'csp-header';
  header.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="14" y="3" rx="1"/><path d="M10 21V8a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5a1 1 0 0 0-1-1H3"/></svg>Abilities';
  picker.appendChild(header);

  const list = document.createElement('div');
  list.className = 'csp-list';
  picker.appendChild(list);
  document.body.appendChild(picker);
  _pickerEl = picker;
  _pickerList = list;

  document.addEventListener('click', (e) => {
    if (_pickerEl && _pickerEl.style.display !== 'none'
        && !_pickerEl.contains(e.target)
        && e.target !== _btnEl
        && !(_btnEl && _btnEl.contains(e.target))) {
      _pickerEl.style.display = 'none';
    }
  });
  picker.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { _pickerEl.style.display = 'none'; }
  });
}

function _togglePicker() {
  if (!_pickerEl) _buildPicker();
  if (_pickerEl.style.display !== 'none') {
    _pickerEl.style.display = 'none';
    return;
  }
  const footer = (document.getElementById('chat-footer-row') && document.getElementById('chat-footer-row').classList.contains('expanded'))
    ? document.getElementById('chat-footer-row')
    : document.getElementById('chat-input-row');
  if (!footer) return;
  const rect = footer.getBoundingClientRect();
  _pickerEl.style.top = 'auto';
  _pickerEl.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
  _pickerEl.style.left = 'auto';
  _pickerEl.style.right = Math.max(4, Math.min(window.innerWidth - rect.right, window.innerWidth - 240)) + 'px';
  setTimeout(() => { _pickerEl.style.display = 'block'; }, 0);
  _renderPickerList();
}

function _renderPickerList() {
  if (!_pickerList) return;
  const list = _cached || [];
  _pickerList.innerHTML = '';
  if (!list.length) {
    _pickerList.innerHTML = '<div class="csp-empty">No abilities enabled</div>';
    return;
  }
  list.forEach(a => {
    const item = document.createElement('div');
    const isActive = !!a.active;
    // Active → normal colour + highlighted row; idle → dull + unhighlighted.
    // No check mark: the highlight alone signals on/off.
    item.className = 'csp-item ' + (isActive ? 'csp-active' : 'csp-idle');
    item.dataset.abilityId = a.id;

    const nameSpan = document.createElement('span');
    nameSpan.className = 'csp-item-name';
    nameSpan.textContent = a.display_name || a.id;
    item.appendChild(nameSpan);

    item.title = isActive
      ? 'On for this chat — click to turn off'
      : 'Off — click to turn on for this chat';
    item.addEventListener('click', () => _onToggleAbility(a, item));
    _pickerList.appendChild(item);
  });
}

async function _onToggleAbility(a, itemEl) {
  if (!app.currentSessionId || !app.currentUserId) return;
  if (itemEl) itemEl.style.opacity = '0.5';
  const endpoint = a.active
    ? '/api/v1/chat/abilities/deactivate'
    : '/api/v1/chat/abilities/activate';
  try {
    await fetch(apiPath(endpoint), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: app.currentSessionId,
        ability_id: a.id,
        agent_id: _agentId(),
      }),
    });
    await refreshActiveAbilities();
    _renderPickerList();
  } catch (_) {
    if (itemEl) itemEl.style.opacity = '';
  }
}

// ── Public refresh ──
async function refreshActiveAbilities() {
  if (_inFlight) return;
  if (!app.currentUserId || !app.currentSessionId) { _updateBadge(0); return; }
  const sessionId = app.currentSessionId;
  if (!_sessionsCache.some((session) => session.id === sessionId && session.created_at !== null)) { _cached = []; _updateBadge(0); return; }
  if (_missingSessionId && _missingSessionId !== sessionId) _missingSessionId = null;
  if (_missingSessionId === sessionId) { _updateBadge(0); return; }

  _inFlight = true;
  try {
    const params = new URLSearchParams({
      user_id: app.currentUserId,
      session_id: app.currentSessionId,
    });
    const aid = _agentId();
    if (aid) params.set('agent_id', aid);
    const resp = await fetch(apiPath('/api/v1/chat/abilities?') + params.toString(), {
      headers: { ...authHeaders() },
    });
    if (resp.status === 404) {
      _missingSessionId = sessionId;
      _cached = [];
      _updateBadge(0);
      return;
    }
    const data = await resp.json().catch(() => ({}));
    _cached = data.abilities || [];
    _updateBadge((_cached || []).filter(a => a.active).length);
    if (_pickerEl && _pickerEl.style.display !== 'none') {
      _renderPickerList();
    }
  } catch (_) {
    _updateBadge(0);
  } finally {
    _inFlight = false;
  }
}

// ── Init ──
export function initAbilities() {
  _btnEl = document.getElementById('chat-abilities-btn');
  _badgeEl = document.getElementById('chat-abilities-badge');
  if (_btnEl) {
    _btnEl.addEventListener('click', (e) => {
      e.stopPropagation();
      _togglePicker();
    });
  }
  app.refreshActiveAbilities = refreshActiveAbilities;
  refreshActiveAbilities();
}

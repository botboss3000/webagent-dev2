'use strict';

import {
  appendChatSurfaceBubble,
  applyChatSurfaceProfile,
  applyChatSurfaceHeader,
  chatSurfaceStatsHtml,
  createChatSurfaceUsage,
  installChatSurfaceIcons,
  wireChatSurfaceComposer,
  wireStatsCarousel,
} from '../shared/js/chat-surface.js';

// Standalone embed chat client. No imports from the SPA — talks straight to the
// public HTTP API on the SAME origin it was served from:
//   POST /api/v1/agents/<id>/auth/guest     → mint an agent-scoped guest
//   POST /api/v2/chat/sessions/<id>/messages → controlled turn
// Config + agent id arrive in window.__EMBED__ (server-injected). Runs inside
// the customer's iframe; talks to the host page's loader via postMessage.

const BOOT = window.__EMBED__ || {};
const CFG = BOOT.config || {};
const AGENT_ID = BOOT.agentId;
const VIEW = CFG.chat_ui || {};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));
	const els = {
	  root: $('ec-root'),
	  messages: $('ec-messages'),
	  header: document.querySelector('.ec-header'),
	  title: $('ec-title'),
	  subtitle: $('ec-subtitle'),
	  composer: $('ec-composer'),
	  input: $('ec-input'),
	  send: $('ec-send'),
	  close: $('ec-close'),
	  unavailable: $('ec-unavailable'),
	  unavailableText: $('ec-unavailable-text'),
	  pill: document.querySelector('.ec-pill'),
	  stats: document.querySelector('.ec-stats'),
	  pillButtons: document.querySelector('.ec-pill-buttons'),
	  attach: document.querySelector('.ec-attach'),
	  voice: document.querySelector('.ec-voice'),
	  above: document.querySelector('.ec-above-pill'),
	  below: document.querySelector('.ec-below-pill'),
	  stop: $('ec-stop'),
	  continue: $('ec-continue'),
	  headIcon: document.querySelector('.ec-head-icon'),
	  stopIcon: document.querySelector('.ec-stop-icon'),
	  continueIcon: document.querySelector('.ec-continue-icon'),
	  toggleIcon: document.querySelector('.ec-toggle-icon'),
	  abilitiesIcon: document.querySelector('.ec-abilities-icon'),
	  targetIcon: document.querySelector('.ec-target-icon'),
	  footerHandle: document.querySelector('.chat-footer-handle'),
	  activityText: document.querySelector('.chat-activity-text'),
	  dot: document.querySelector('.cw-dot'),
	  closeBtn: $('ec-close'),
	};
els.stats.innerHTML = chatSurfaceStatsHtml();
wireStatsCarousel(els.stats);
const usage = createChatSurfaceUsage(els.stats);

// ── Theming ────────────────────────────────────────────────────────────────
function applyTheme() {
  const accent = CFG.accent || '#4f46e5';
  document.documentElement.style.setProperty('--ec-accent', accent);
  document.documentElement.style.setProperty('--ec-accent-fg', contrastColor(accent));
  document.documentElement.style.setProperty('--brand', accent);
  document.documentElement.style.setProperty('--accent', accent);
  els.title.textContent = CFG.title || BOOT.agentName || 'Chat';
  if (CFG.subtitle) { els.subtitle.textContent = CFG.subtitle; els.subtitle.hidden = false; }
  if (CFG.placeholder) els.input.setAttribute('placeholder', CFG.placeholder);
  applyViewProfile();
}

// The embed consumes the exact same widget profile as the in-app widget.
function applyViewProfile() {
  applyChatSurfaceProfile(els, VIEW);
}

// Pick black or white text for a given hex background (WCAG-ish luminance).
function contrastColor(hex) {
  let h = (hex || '').replace('#', '');
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  if (h.length !== 6) return '#ffffff';
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const L = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  return L > 0.5 ? '#1a1a24' : '#ffffff';
}

// ── Parent-frame messaging ──────────────────────────────────────────────────
function toParent(action, data) {
  try { parent.postMessage(Object.assign({ source: 'webagent-embed', action }, data || {}), '*'); }
  catch (_) { /* not framed / cross-origin locked — harmless */ }
}

// ── Message rendering ────────────────────────────────────────────────────────
function addBubble(role, text) {
  const el = appendChatSurfaceBubble(els.messages, role, text || '');
  el.classList.add('ec-msg');
  return el;
}

function showTyping() {
  const el = document.createElement('div');
  el.className = 'chat-bubble ec-msg agent';
  el.innerHTML = '<span class="ec-typing"><span></span><span></span><span></span></span>';
  els.messages.appendChild(el);
  scrollToEnd();
  return el;
}

function scrollToEnd() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

// ── Session bootstrap (lazy: minted on first send) ──────────────────────────
let session = null;      // { token, user_id, session_id }
let sending = false;

function browserId() {
  let id = null;
  try { id = localStorage.getItem('webagent_embed_bid'); } catch (_) {}
  if (!id) {
    id = (crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    try { localStorage.setItem('webagent_embed_bid', id); } catch (_) {}
  }
  return id;
}

function guestCredentialKey() {
  return `webagent_embed_guest:${AGENT_ID}`;
}

function memberSessionKey() {
  return `webagent_embed_member:${AGENT_ID}`;
}

async function ensureSession() {
  if (session) return session;
  try {
    const saved = JSON.parse(localStorage.getItem(memberSessionKey()) || 'null');
    if (saved?.token && saved?.user_id) {
      const check = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}/auth/me`, {
        headers: { 'Authorization': `Bearer ${saved.token}` },
      });
      if (check.ok) {
        const state = await check.json();
        if (state.authenticated) {
          session = { token: saved.token, user_id: saved.user_id, session_id: saved.session_id || saved.user_id };
          return session;
        }
      }
      localStorage.removeItem(memberSessionKey());
    }
  } catch (_) { /* private storage unavailable or stale */ }
  const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}/auth/guest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      browser_id: browserId(),
      guest_credential: (() => {
        try { return localStorage.getItem(guestCredentialKey()) || null; } catch (_) { return null; }
      })(),
    }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Could not start chat (${res.status}).`);
  }
  session = await res.json();
  if (session.guest_credential) {
    try { localStorage.setItem(guestCredentialKey(), session.guest_credential); } catch (_) {}
  }
  return session;
}

function authCard(payload) {
  document.querySelectorAll('.ec-agent-auth').forEach((node) => node.remove());
  const card = document.createElement('form');
  card.className = 'chat-bubble ec-msg agent ec-agent-auth';
  card.autocomplete = 'on';
  card.innerHTML = `
    <strong>Sign in to ${esc(CFG.title || BOOT.agentName || 'this agent')}</strong>
    <label>Username or email<input name="username" autocomplete="username" required minlength="3"></label>
    <label>Password<input name="password" type="password" autocomplete="current-password" required minlength="8"></label>
    ${payload.local_signup_mode === 'invite' ? '<label>Invitation code<input name="invite_code" autocomplete="one-time-code"></label>' : ''}
    <div class="ec-auth-actions">
      <button type="submit">Sign in</button>
      ${['open', 'invite'].includes(payload.local_signup_mode) ? '<button type="button" data-auth-register>Create account</button>' : ''}
      ${payload.app_login_enabled ? '<button type="button" data-auth-app>Use app account</button>' : ''}
    </div><small class="ec-auth-status">Credentials are sent directly to the authentication service and are not added to chat.</small>`;
  Object.assign(card.style, { display: 'grid', gap: '8px', padding: '14px' });
  card.querySelectorAll('label').forEach((label) => Object.assign(label.style, { display: 'grid', gap: '4px', fontSize: '12px' }));
  card.querySelectorAll('input').forEach((input) => Object.assign(input.style, { padding: '9px', borderRadius: '8px', border: '1px solid #8886', background: 'transparent', color: 'inherit' }));
  const status = card.querySelector('.ec-auth-status');
  const finishAuth = (data) => {
    session = { token: data.token, user_id: data.user_id, session_id: data.session_id || data.user_id };
    try { localStorage.setItem(memberSessionKey(), JSON.stringify(session)); } catch (_) {}
    card.remove();
    addBubble('agent', `Signed in as ${data.principal?.display_name || data.principal?.profile?.name || 'agent member'}.`);
  };
  const submitLocal = async (register) => {
    const form = new FormData(card);
    status.textContent = register ? 'Creating account…' : 'Signing in…';
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}/auth/${register ? 'register' : 'login'}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: form.get('username'), password: form.get('password'), display_name: form.get('username'),
        guest_credential: (() => { try { return localStorage.getItem(guestCredentialKey()) || ''; } catch (_) { return ''; } })(),
        invite_code: form.get('invite_code') || '' }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Sign-in failed.');
    finishAuth(data);
  };
  card.addEventListener('submit', (event) => { event.preventDefault(); submitLocal(false).catch((err) => { status.textContent = err.message; }); });
  card.querySelector('[data-auth-register]')?.addEventListener('click', () => submitLocal(true).catch((err) => { status.textContent = err.message; }));
  card.querySelector('[data-auth-app]')?.addEventListener('click', async () => {
    try {
      const appToken = localStorage.getItem('auth_token') || '';
      if (!appToken) throw new Error('Open this app and sign in first, then return here.');
      const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}/auth/app-link`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${appToken}` },
        body: JSON.stringify({ guest_credential: (() => { try { return localStorage.getItem(guestCredentialKey()) || ''; } catch (_) { return ''; } })() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'App-account sign-in failed.');
      finishAuth(data);
    } catch (err) { status.textContent = err.message; }
  });
  els.messages.appendChild(card);
  scrollToEnd();
  card.querySelector('input')?.focus();
}

function profileAdminCard(initial) {
  document.querySelectorAll('.ec-agent-profile-admin').forEach((node) => node.remove());
  const card = document.createElement('section');
  card.className = 'chat-bubble ec-msg agent ec-agent-profile-admin';
  Object.assign(card.style, { display: 'grid', gap: '10px', padding: '14px', maxWidth: '95%' });
  const profiles = initial.profiles || [];
  const members = initial.members || [];
  const options = profiles.filter((p) => p.slug !== 'agent-administrator')
    .map((p) => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`).join('');
  card.innerHTML = `<strong>Agent profiles</strong>
    <form data-new-profile style="display:grid;gap:6px">
      <input name="name" placeholder="New profile name" required>
      <input name="slug" placeholder="profile-slug" required>
      <input name="abilities" placeholder="Allowed abilities (comma separated, or *)">
      <input name="tools" placeholder="Allowed tools (comma separated, or *)">
      <button type="submit">Add profile</button>
    </form>
    <div data-members>${members.map((m) => `<div data-member="${esc(m.member_id)}" style="display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center">
      <span>${esc(m.display_name || m.member_id.slice(0, 8))} · ${esc(m.profile?.name || '')}</span>
      <select data-profile ${m.is_agent_admin ? 'disabled' : ''}>${options}</select>
      <label><input type="checkbox" data-admin ${m.is_agent_admin ? 'checked' : ''}> administrator</label>
    </div>`).join('')}</div>
    <form data-policy style="display:grid;gap:6px"><label><input type="checkbox" name="app_login_enabled" ${initial.auth_policy?.app_login_enabled ? 'checked' : ''}> Accept app-account login</label>
      <label>Local signup <select name="local_signup_mode"><option>open</option><option>invite</option><option>disabled</option></select></label>
      <label><input type="checkbox" name="transcript_review" ${initial.auth_policy?.transcript_review ? 'checked' : ''}> Disclosed administrator transcript review</label>
      <button type="submit">Save access policy</button></form><small data-admin-status></small>`;
  card.querySelectorAll('input,select').forEach((el) => Object.assign(el.style, { padding: '7px', borderRadius: '7px', border: '1px solid #8886', background: 'transparent', color: 'inherit' }));
  const auth = () => ({ 'Content-Type': 'application/json', 'Authorization': `Bearer ${session?.token || ''}` });
  const call = async (path, body) => {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}${path}`, { method: 'PUT', headers: auth(), body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Update failed.');
    return data;
  };
  const status = card.querySelector('[data-admin-status]');
  card.querySelector('[data-policy] select').value = initial.auth_policy?.local_signup_mode || 'open';
  card.querySelector('[data-new-profile]').addEventListener('submit', async (event) => {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    const slug = f.get('slug');
    try { await call(`/profiles/${encodeURIComponent(slug)}`, { slug, name: f.get('name'), policy: {
      abilities: String(f.get('abilities') || '').split(',').map((v) => v.trim()).filter(Boolean),
      tools: String(f.get('tools') || '').split(',').map((v) => v.trim()).filter(Boolean),
    }}); status.textContent = 'Profile saved. Ask me to show profile administration again to refresh.'; }
    catch (err) { status.textContent = err.message; }
  });
  card.querySelector('[data-policy]').addEventListener('submit', async (event) => {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    try { await call('/auth/policy', { app_login_enabled: f.has('app_login_enabled'), local_signup_mode: f.get('local_signup_mode'), transcript_review: f.has('transcript_review') }); status.textContent = 'Access policy saved.'; }
    catch (err) { status.textContent = err.message; }
  });
  card.querySelectorAll('[data-member]').forEach((row) => {
    const memberId = row.dataset.member;
    const select = row.querySelector('[data-profile]');
    if (select) select.value = members.find((m) => m.member_id === memberId)?.profile?.slug || 'member';
    select?.addEventListener('change', () => call(`/profile-members/${encodeURIComponent(memberId)}/profile`, { profile_slug: select.value }).then(() => { status.textContent = 'Member profile updated.'; }).catch((e) => { status.textContent = e.message; }));
    row.querySelector('[data-admin]')?.addEventListener('change', (event) => call(`/profile-members/${encodeURIComponent(memberId)}/administrator`, { enabled: event.target.checked }).then(() => { status.textContent = 'Administrator role updated.'; }).catch((e) => { event.target.checked = !event.target.checked; status.textContent = e.message; }));
  });
  els.messages.appendChild(card); scrollToEnd();
}

// ── Controlled v2 conversation ─────────────────────────────────────────────
const API = '/api/v2/chat/sessions/';
const published = new Map();
let activeRunId = null;

async function v2Request(path, options = {}) {
  const s = await ensureSession();
  const response = await fetch(path, {
    ...options,
    headers: { 'Authorization': `Bearer ${s.token}`, 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Chat failed (${response.status}).`);
  }
  return response;
}

function showPublication(item) {
  if (!item || item.deleted) return;
  if (item.kind === 'component' && item.payload?.ui === 'agent_auth') {
    const previous = published.get(item.id);
    if (previous && previous.revision >= item.revision) return;
    authCard(item.payload);
    published.set(item.id, { revision: item.revision });
    return;
  }
  if (!['user', 'answer', 'notice', 'failure'].includes(item.kind)) return;
  if (typeof item.payload?.text !== 'string') return;
  const previous = published.get(item.id);
  if (previous && previous.revision >= item.revision) return;
  const bubble = previous?.bubble || addBubble(item.kind === 'user' ? 'user' : item.kind === 'failure' ? 'error' : 'agent', item.payload.text);
  bubble.textContent = item.payload.text;
  published.set(item.id, { revision: item.revision, bubble });
  scrollToEnd();
}

async function readEvents(path, cursor) {
  const response = await v2Request(path + '/events?after=' + cursor);
  const frames = (await response.text()).split(/\r?\n\r?\n/);
  const events = [];
  for (const frame of frames) {
    const type = frame.split(/\r?\n/).find(line => line.startsWith('event:'))?.slice(6).trim();
    const data = frame.split(/\r?\n/).find(line => line.startsWith('data:'))?.slice(5).trim();
    if (type && data) {
      try { events.push({ type, value: JSON.parse(data) }); } catch (_) {}
    }
  }
  return events;
}

async function sendMessage(text) {
  if (sending || !text.trim()) return;
  sending = true;
  els.root.setAttribute('aria-busy', 'true');
  els.activityText.textContent = 'Working…';
  usage.begin();
  if (els.below.classList.contains('expanded')) els.below.hidden = false;
  if ((VIEW.above_pill?.right || []).includes('stop')) els.stop.hidden = false;
  els.continue.hidden = true;
  els.send.disabled = true;
  els.input.value = '';
  els.pill.classList.remove('has-text');
  autoGrow();
  const optimistic = addBubble('user', text);
  const typing = showTyping();
  let accepted = false;

  try {
    const s = await ensureSession();
    const path = API + encodeURIComponent(s.session_id);
    await v2Request('/api/v2/chat/sessions', {
      method: 'POST',
      body: JSON.stringify({ id: s.session_id, agent_id: AGENT_ID, title: 'Embed chat' }),
    });
    const receipt = await (await v2Request(path + '/messages', {
      method: 'POST',
      body: JSON.stringify({ client_message_id: crypto.randomUUID(), text, execution_mode: 'ask' }),
    })).json();
    accepted = true;
    activeRunId = receipt.run_id;
    const snapshot = await (await v2Request(path)).json();
    let cursor = snapshot.cursor;
    let done = snapshot.runs.some(run => run.id === activeRunId &&
      ['completed', 'cancelled', 'failed'].includes(run.state));
    for (const item of snapshot.items) {
      if (item.id === receipt.message_id) {
        published.set(item.id, { revision: item.revision, bubble: optimistic });
      } else {
        showPublication(item);
      }
    }
    while (!done) {
      const events = await readEvents(path, cursor);
      for (const event of events) {
        if (event.type === 'revoked') throw new Error('Chat access ended.');
        if (event.type === 'publication') {
          cursor = Math.max(cursor, event.value.cursor);
          showPublication(event.value.item);
        }
        if (event.type === 'state') done = event.value.runs.some(run => run.id === activeRunId &&
          ['completed', 'cancelled', 'failed'].includes(run.state));
      }
      if (!events.length) {
        const current = await (await v2Request(path)).json();
        for (const item of current.items) showPublication(item);
        cursor = Math.max(cursor, current.cursor);
        done = current.runs.some(run => run.id === activeRunId &&
          ['completed', 'cancelled', 'failed'].includes(run.state));
      }
    }
    if (!done) await new Promise(resolve => setTimeout(resolve, 300));
  } catch (err) {
    if (!accepted) optimistic.remove();
    addBubble('error', (err && err.message) || 'Something went wrong. Please try again.');
  } finally {
    if (typing.isConnected) typing.remove();
    activeRunId = null;
    usage.finish();
    sending = false;
    els.root.setAttribute('aria-busy', 'false');
    els.activityText.textContent = '';
    els.stop.hidden = true;
    if ((VIEW.above_pill?.right || []).includes('continue')) els.continue.hidden = false;
    els.send.disabled = !els.input.value.trim();
    els.input.focus();
  }
}

// ── Input UX ────────────────────────────────────────────────────────────────
function autoGrow() {
  els.input.style.height = 'auto';
  const maxHeight = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--ec-input-max-height'), 10) || 120;
  els.input.style.height = Math.min(els.input.scrollHeight, maxHeight) + 'px';
}

function wireEvents() {
  els.composer.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(els.input.value); });
  els.input.addEventListener('input', autoGrow);
  els.continue.addEventListener('click', () => sendMessage('continue'));
  els.stop.addEventListener('click', () => {
    if (!activeRunId || !session) return;
    v2Request(API + encodeURIComponent(session.session_id) + '/stop', { method: 'POST' })
      .catch(err => addBubble('error', err.message || 'Could not stop this turn.'));
  });
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (window.matchMedia?.('(pointer: coarse)').matches) return;
      e.preventDefault();
      sendMessage(els.input.value);
    }
  });
  els.close.addEventListener('click', () => toParent('close'));
  wireChatSurfaceComposer(els, { isBusy: () => sending });
}

// ── Init ────────────────────────────────────────────────────────────────────
function init() {
  installChatSurfaceIcons(els, CFG.agent_icon || 'bot');
  applyTheme();
  wireEvents();
  toParent('ready');

  if (!BOOT.embeddable) {
    els.composer.hidden = true;
    els.messages.hidden = true;
    els.unavailable.hidden = false;
    els.unavailableText.textContent = !BOOT.enabled
      ? 'This chat widget has not been enabled by its owner.'
      : 'This agent is not open to anonymous visitors.';
    return;
  }
  if (CFG.greeting) addBubble('agent', CFG.greeting);
  els.input.focus();
}

init();

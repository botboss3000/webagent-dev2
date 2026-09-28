'use strict';

// ── Embed chat — standalone chat client for iframe embedding ──────────────
// Self-contained: no app-shell dependencies. Reads agent ID from the URL path
// (/embed/{agent_id}), creates an anonymous session, opens a WebSocket, and
// renders chat bubbles. CSS custom properties on :root allow per-agent styling.

const AGENT_ID = window.location.pathname.replace(/^\/embed\//, '').replace(/\/$/, '');
const WS_URL = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/api/v1/agent/ws';

let token = '';
let userId = '';
let sessionId = '';
let ws = null;
let reconnectTimer = null;
let reconnectDelay = 500;
const MAX_RECONNECT_DELAY = 30000;

const els = {
  headerName: document.getElementById('embed-agent-name'),
  headerIcon: document.getElementById('embed-agent-icon'),
  chat: document.getElementById('embed-chat'),
  input: document.getElementById('embed-input'),
  sendBtn: document.getElementById('embed-send-btn'),
};

let running = false;
let turnBuffers = new Map();
let bubbles = new Map();
let lastSeq = 0;
let reconcileTimer = null;
let reconcileInFlight = false;

// ── Init ──

async function init() {
  if (!AGENT_ID) {
    showError('No agent ID in URL.');
    return;
  }

  try {
    // Create anonymous session
    const browserId = 'embed_' + (localStorage.getItem('embed_browser_id') || crypto.randomUUID());
    localStorage.setItem('embed_browser_id', browserId);

    const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}/anon-session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser_id: browserId }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || 'Agent not available for embedding.');
      return;
    }

    const data = await res.json();
    token = data.token;
    userId = data.user_id;
    sessionId = data.session_id;

    // Fetch agent info for the header
    try {
      const aRes = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}?user_id=${encodeURIComponent(userId)}`, {
        headers: { 'Authorization': `Bearer ${token}` },
      });
      if (aRes.ok) {
        const aData = await aRes.json();
        const agent = aData.agent || {};
        els.headerName.textContent = agent.name || 'Agent';
        if (agent.icon) {
          els.headerIcon.textContent = agent.icon;
        } else {
          els.headerIcon.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14l2 2 4-4"/></svg>';
        }
      }
    } catch (_) {
      els.headerName.textContent = 'Agent';
    }

    // Apply embed config from agent metadata
    try {
      applyEmbedConfig();
    } catch (_) {}

    // Connect WebSocket
    connectWs();

    // Wire input
    wireInput();

    // Show welcome
    addBubble('agent', 'Hi! How can I help you today?');

  } catch (e) {
    showError('Failed to connect: ' + e.message);
  }
}

async function applyEmbedConfig() {
  // Fetch agent to get embed_config from metadata
  try {
    const res = await fetch(`/api/v1/agents/${encodeURIComponent(AGENT_ID)}?user_id=${encodeURIComponent(userId)}`, {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (!res.ok) return;
    const data = await res.json();
    const agent = data.agent || {};
    const cfg = (agent.embed_config && typeof agent.embed_config === 'object') ? agent.embed_config : {};
    const root = document.documentElement;
    if (cfg.primary_color) root.style.setProperty('--embed-accent', cfg.primary_color);
    if (cfg.bg_color) root.style.setProperty('--embed-bg', cfg.bg_color);
    if (cfg.text_color) root.style.setProperty('--embed-fg', cfg.text_color);
    if (cfg.font_family) root.style.setProperty('--embed-font', cfg.font_family);
    if (cfg.border_radius) root.style.setProperty('--embed-radius', cfg.border_radius);
    if (cfg.title) els.headerName.textContent = cfg.title;
    if (cfg.custom_css) {
      const style = document.createElement('style');
      style.textContent = cfg.custom_css;
      document.head.appendChild(style);
    }
  } catch (_) {}
}

// ── WebSocket ──

function connectWs() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    reconnectDelay = 500;
    // Send the proper handshake — same protocol as the main app
    ws.send(JSON.stringify({
      mode: 'user_subscriber',
      user_id: userId,
      token: token,
      resume: {},
    }));
    startReconcile();
  };

  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      handleEvent(ev);
    } catch (_) {}
  };

  ws.onclose = () => {
    stopReconcile();
    scheduleReconnect();
  };

  ws.onerror = () => {
    ws.close();
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    connectWs();
  }, reconnectDelay);
}

// ── Event handling ──

function handleEvent(ev) {
  if (!ev) return;

  // Handshake confirmation
  if (ev.type === 'subscribed') return;
  if (ev.type === 'ping') return;

  // Only process events for our session
  const evSid = ev.session_id || ev.sessionId;
  if (evSid !== sessionId) return;

  const key = ev.asst_id || ev.turn_id || ev.id || '';

  switch (ev.type) {
    case 'user_message':
      if (ev.genui_label) {
        // GenUI-originated page sends: green label notice, never the raw prompt.
        const b = addBubble('info', ev.genui_label);
        b.classList.add('system-genui');
      } else {
        addBubble('user', ev.content || '');
      }
      running = true;
      break;
    case 'stream':
      if (!key) break;
      seedStreaming(key, (turnBuffers.get(key) || '') + (ev.content || ''));
      running = true;
      break;
    case 'agent_step_end':
      finalizeKey(key, ev.content || turnBuffers.get(key) || '');
      break;
    case 'response':
      finalizeKey(key, ev.content || turnBuffers.get(key) || '');
      settleTurn();
      break;
    case 'interrupted':
      markInterrupted(key);
      settleTurn();
      break;
    case 'error':
      addBubble('agent', 'Error: ' + (ev.message || 'unknown'));
      settleTurn();
      break;
    default:
      break;
  }
}

// ── Bubbles ──

function addBubble(role, text) {
  const b = document.createElement('div');
  b.className = 'embed-bubble ' + role;
  b.textContent = text || '';
  els.chat.appendChild(b);
  scrollDown();
  return b;
}

function seedStreaming(key, text) {
  if (!key) return;
  let bubble = bubbles.get(key);
  if (!bubble) {
    bubble = addBubble('agent', '');
    bubble.classList.add('streaming');
    bubble.dataset.key = key;
    bubbles.set(key, bubble);
  }
  turnBuffers.set(key, text);
  bubble.textContent = text;
  scrollDown();
}

function finalizeKey(key, text) {
  let bubble = key ? bubbles.get(key) : null;
  const clean = (text || '').trim();
  if (!clean) { if (bubble) { bubble.remove(); if (key) bubbles.delete(key); } return; }
  if (!bubble) bubble = addBubble('agent', '');
  bubble.classList.remove('streaming');
  if (key) bubble.dataset.key = key;
  bubble.innerHTML = renderMarkdown(clean);
  if (key) { bubbles.set(key, bubble); turnBuffers.delete(key); }
  scrollDown();
}

function markInterrupted(key) {
  let bubble = key ? bubbles.get(key) : null;
  if (!bubble) {
    const streaming = els.chat.querySelectorAll('.embed-bubble.agent.streaming');
    bubble = streaming[streaming.length - 1] || null;
  }
  if (bubble) {
    const cur = (key && turnBuffers.get(key)) || bubble.textContent || '';
    bubble.classList.remove('streaming');
    bubble.innerHTML = renderMarkdown(cur ? cur + '\n\n_(stopped)_' : '_(stopped)_');
  }
  if (key) turnBuffers.delete(key);
}

function settleTurn() {
  running = false;
  stopReconcile();
  els.sendBtn.disabled = false;
}

// ── Simple markdown renderer ──

function renderMarkdown(text) {
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Code blocks
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return '<pre><code>' + code.replace(/\n$/, '') + '</code></pre>';
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // Italic
  html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

  // Unordered lists
  html = html.replace(/^[\t ]*[-*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');

  // Ordered lists
  html = html.replace(/^[\t ]*\d+\. (.+)$/gm, '<li>$1</li>');

  // Paragraphs (double newlines)
  html = html.replace(/\n\n/g, '</p><p>');
  html = '<p>' + html + '</p>';

  // Clean up empty paragraphs
  html = html.replace(/<p><\/p>/g, '');

  return html;
}

// ── DB reconcile (durable path) ──

function startReconcile() {
  if (reconcileTimer) return;
  reconcileTimer = setInterval(reconcileTick, 800);
}

function stopReconcile() {
  if (reconcileTimer) { clearInterval(reconcileTimer); reconcileTimer = null; }
}

async function reconcileTick() {
  if (!running || reconcileInFlight) return;
  reconcileInFlight = true;
  try {
    const url = `/api/v1/db/session-tail?db=user.db&session_id=${encodeURIComponent(sessionId)}`
      + `&after_session_seq=${lastSeq}&user_id=${encodeURIComponent(userId)}&token=${encodeURIComponent(token)}`;
    const res = await fetch(url);
    if (!res.ok) { reconcileInFlight = false; return; }
    const data = await res.json();
    if (!data || data.restricted) { reconcileInFlight = false; return; }

    let maxSeq = lastSeq;
    const msgs = Array.isArray(data.messages) ? data.messages : [];
    for (const msg of msgs) {
      if (typeof msg.session_seq === 'number') maxSeq = Math.max(maxSeq, msg.session_seq);
      if (msg.role === 'user') {
        const mid = msg.id || '';
        const cont = (msg.content || '').trim();
        if (mid && els.chat.querySelector(`.embed-bubble.user[data-msg-id="${CSS.escape(String(mid))}"]`)) continue;
        const users = els.chat.querySelectorAll('.embed-bubble.user');
        let dup = false;
        for (const u of users) { if (u.textContent.trim() === cont) { if (mid) u.setAttribute('data-msg-id', String(mid)); dup = true; break; } }
        if (!dup) {
          const b = addBubble('user', msg.content || '');
          if (mid) b.setAttribute('data-msg-id', String(mid));
        }
        continue;
      }
      if (msg.role !== 'assistant') continue;
      const key = msg.id;
      const text = stripToolCalls(msg.content || '');
      if (msg.status === 'streaming') seedStreaming(key, text);
      else if (msg.status === 'interrupted') markInterrupted(key);
      else finalizeKey(key, text);
    }

    const run = data.run || null;
    if (run && run.active) running = true;
    lastSeq = Math.max(lastSeq, maxSeq,
      (run && typeof run.latest_session_seq === 'number') ? run.latest_session_seq : 0);

    if (run && run.active === false && !running) {
      stopReconcile();
      els.sendBtn.disabled = false;
    }
  } catch (_) {} finally {
    reconcileInFlight = false;
  }
}

function stripToolCalls(text) {
  const idx = (text || '').indexOf('\n\n[Tool calls: ');
  return idx !== -1 ? text.slice(0, idx) : text;
}

// ── Input ──

function wireInput() {
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      // On touch devices (mobile), let Enter insert a newline; send via the button.
      if (window.matchMedia?.('(pointer: coarse)').matches) return;
      e.preventDefault();
      send();
    }
  });

  els.input.addEventListener('input', () => {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(els.input.scrollHeight, 120) + 'px';
  });

  els.sendBtn.addEventListener('click', send);
}

async function send() {
  const text = els.input.value.trim();
  if (!text || !token) return;
  if (text.length > 32000) {
    addBubble('agent', 'This message is too large. Anonymous messages are limited to 32,000 characters.');
    return;
  }

  els.input.value = '';
  els.input.style.height = 'auto';
  els.sendBtn.disabled = true;

  addBubble('user', text);
  running = true;
  startReconcile();

  try {
    await fetch('/api/v1/chat/send', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({
        message: text,
        session_id: sessionId,
        user_id: userId,
        agent_id: AGENT_ID,
        execution_mode: 'auto',
      }),
    });
  } catch (e) {
    addBubble('agent', 'Failed to send message. Please try again.');
    running = false;
    els.sendBtn.disabled = false;
  }
}

// ── Helpers ──

function scrollDown() {
  els.chat.scrollTop = els.chat.scrollHeight;
}

function showError(msg) {
  const el = document.createElement('div');
  el.className = 'embed-error';
  el.textContent = msg;
  els.chat.appendChild(el);
}

// ── Start ──

init();

'use strict';

import { partialsReady } from '../shared/js/partial-loader.js';

const cfg = window.__CHAT_PORTAL_CONFIG__ || {};

function notify(action, data) {
  try {
    parent.postMessage({ source: 'webagent-embed', action, ...(data || {}) }, '*');
  } catch (_) {}
}

function forcePanelVisible() {
  document.body.classList.add('chat-visible');
  document.body.classList.remove('chat-hidden');
  try { window.__applyChatVisible?.(true, false); } catch (_) {}
  const panel = document.getElementById('chat-panel');
  if (panel) panel.style.removeProperty('display');
}

function updateWidgetHeader() {
  const title = document.querySelector('[data-element-origin="title"], [data-header-control="title"]');
  const titleText = cfg.title || window.__agentName || 'Chat';
  if (title && title.textContent !== titleText) title.textContent = titleText;

  const icon = document.querySelector('[data-element-origin="icon"], [data-header-control="icon"]');
  if (icon && cfg.agent_icon && icon.dataset.portalIcon !== cfg.agent_icon) {
    icon.dataset.portalIcon = cfg.agent_icon;
    icon.setAttribute('data-lucide', cfg.agent_icon);
    try { window.lucide?.createIcons({ nodes: [icon] }); } catch (_) {}
  }

  const thinking = document.getElementById('chat-input-row')?.classList.contains('thinking')
    || window.__agentTurnActive === true;
  const status = document.querySelector('[data-element-origin="status"], [data-header-control="status"]');
  const dot = document.querySelector('[data-element-origin="dot"], [data-header-control="dot"]');
  const statusText = thinking ? 'Working\u2026' : '';
  if (status && status.textContent !== statusText) status.textContent = statusText;
  if (dot) {
    dot.classList.toggle('running', thinking);
    dot.classList.remove('done', 'error');
  }
}

async function init() {
  await partialsReady;
  forcePanelVisible();

  document.addEventListener('chat-control:close', () => notify('close'));
  document.addEventListener('chat-control:minimize', () => notify('close'));

  const input = document.getElementById('chat-input-row');
  if (input && typeof MutationObserver === 'function') {
    new MutationObserver(updateWidgetHeader).observe(input, {
      attributes: true,
      attributeFilter: ['class'],
    });
  }

  const panel = document.getElementById('chat-panel');
  if (panel && typeof MutationObserver === 'function') {
    new MutationObserver(updateWidgetHeader).observe(panel, { childList: true, subtree: true });
  }

  const accent = cfg.accent;
  if (accent) {
    document.documentElement.style.setProperty('--brand', accent);
    document.documentElement.style.setProperty('--accent', accent);
  }

  updateWidgetHeader();
  notify('ready');
}

init();

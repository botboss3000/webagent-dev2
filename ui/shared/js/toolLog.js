'use strict';

// Tool-call log panel — logTool(event) renders stream / tool_call / tool_result
// rows into the tool log. Fed by the agent WebSocket (see ui/shared/js/agentWs.js).

import { app } from './state.js';
import { icon } from './icons.js';
import { fetchUrl } from './media-cache.js';

const MAX_TOOL_LOG_ROWS = 300;
const MAX_STREAM_PREVIEW_CHARS = 2_000;
const _streamRows = new Map();
const _pendingStreamText = new Map();
let _streamFrame = null;

function _panelOpen() {
  return !!(app.toolLogPanel && app.toolLogPanel.classList.contains('open'));
}

function _trimRows() {
  if (!app.toolLogContent) return;
  while (app.toolLogContent.children.length > MAX_TOOL_LOG_ROWS) {
    const oldest = app.toolLogContent.firstElementChild;
    if (!oldest) break;
    for (const [key, row] of _streamRows) {
      if (row === oldest) _streamRows.delete(key);
    }
    oldest.remove();
  }
}

function _appendRow(el) {
  if (!app.toolLogContent) return;
  const followTail = _panelOpen()
    && (app.toolLogContent.scrollHeight - app.toolLogContent.scrollTop
        - app.toolLogContent.clientHeight < 48);
  app.toolLogContent.appendChild(el);
  _trimRows();
  if (followTail) app.toolLogContent.scrollTop = app.toolLogContent.scrollHeight;
}

function _flushStreamPreviews() {
  _streamFrame = null;
  for (const [key, text] of _pendingStreamText) {
    const row = _streamRows.get(key);
    if (!row || !row.isConnected) continue;
    const preview = text.length > MAX_STREAM_PREVIEW_CHARS
      ? `…${text.slice(-MAX_STREAM_PREVIEW_CHARS)}` : text;
    const body = row.querySelector('.tl-stream-preview');
    if (body) body.textContent = preview;
  }
  _pendingStreamText.clear();
}

function appendIconText(parent, iconName, text) {
  const iconWrapper = document.createElement('span');
  iconWrapper.innerHTML = icon(iconName, { size: '12px' });
  parent.appendChild(iconWrapper);
  parent.appendChild(document.createTextNode(` ${String(text ?? '')}`));
}

export function logTool(event) {
  // Pipeline lifecycle telemetry belongs to the activity/debug surfaces. Adding
  // one Tool Log DOM node for every step needlessly grows the page during a run.
  if (event.type === 'pipeline') return;

  // One bounded preview per assistant step, not one DOM row per token. Keep the
  // latest tail only; the chat bubble remains the authoritative full response.
  if (event.type === 'stream') {
    if (!app.toolLogContent) return;
    const key = [event.session_id || event.sessionId || '',
      event.asst_id || event.turn_id || 'stream'].join(':');
    let row = _streamRows.get(key);
    if (!row || !row.isConnected) {
      row = document.createElement('div');
      row.className = 'tl-entry tl-think';
      const label = document.createElement('span');
      appendIconText(label, 'message-square', 'Stream:');
      const preview = document.createElement('span');
      preview.className = 'tl-stream-preview';
      row.appendChild(label);
      row.appendChild(preview);
      _streamRows.set(key, row);
      _appendRow(row);
    }
    const previous = _pendingStreamText.get(key)
      || row.querySelector('.tl-stream-preview')?.textContent || '';
    _pendingStreamText.set(key, previous + (event.content || ''));
    if (_streamFrame === null) {
      const schedule = window.requestAnimationFrame || (cb => setTimeout(cb, 16));
      _streamFrame = schedule(_flushStreamPreviews);
    }
    return;
  }

  const el = document.createElement('div');
  el.className = 'tl-entry';

  switch (event.type) {
    case 'tool_call':
      el.className = 'tl-entry tl-call';
      appendIconText(el, 'wrench', `${event.tool || ''}(${JSON.stringify(event.args || {})})`);
      break;
    case 'tool_result': {
      el.className = 'tl-entry ' + (event.error ? 'tl-err' : 'tl-ok');
      const dur = event.duration_ms ? ` [${event.duration_ms}ms]` : '';
      const resultStr = event.result || '';
      const statusIcon = event.error ? icon('x-circle', { size: '12px' }) : icon('check-circle', { size: '12px' });
      if (resultStr.startsWith('/api/v1/content/') || resultStr.startsWith('/screenshots/')) {
        app.lastScreenshotUri = resultStr;
        const label = document.createElement('span');
        label.innerHTML = statusIcon;
        label.appendChild(document.createTextNode(` ${event.tool || ''}${dur}:`));
        const screenshot = document.createElement('img');
        if (resultStr.startsWith('/api/v1/content/')) {
          fetchUrl(`tool-screenshot:${resultStr}`, resultStr).then((url) => {
            if (url) screenshot.src = url;
          });
        } else {
          screenshot.src = resultStr;
        }
        screenshot.alt = 'Tool screenshot';
        screenshot.className = 'tl-screenshot';
        el.appendChild(label);
        el.appendChild(document.createElement('br'));
        el.appendChild(screenshot);
      } else {
        appendIconText(
          el,
          event.error ? 'x-circle' : 'check-circle',
          `${event.tool || ''}${dur}: ${resultStr.slice(0, 300)}`,
        );
      }
      break;
    }
    case 'response':
      el.className = 'tl-entry tl-ok';
      appendIconText(el, 'bot', `Response: ${(event.content || '').slice(0, 200)}`);
      break;
    default:
      el.textContent = JSON.stringify(event);
  }

  _appendRow(el);
}

function initToolLog() {
  app.toolLogToggle.addEventListener('click', () =>
    app.toolLogPanel.classList.toggle('open'),
  );
  app.toolLogClose.addEventListener('click', () =>
    app.toolLogPanel.classList.remove('open'),
  );
}

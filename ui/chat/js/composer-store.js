'use strict';

// Composer external store - stable React-facing snapshot of the legacy composer.
// Existing imperative code remains the send, outbox, draft, and WebSocket authority.
import { app } from '../../shared/js/state.js';

const listeners = new Set();
let snapshot = Object.freeze({ sessionId: '', draft: '', gate: 'loading', phase: 'idle', sending: false, streaming: false, stopped: false, error: false, controls: {}, attachments: 0, activity: '' });
let observer; let scheduled = false;
const control = (id) => { const el = document.getElementById(id); return !!el && !el.hidden && el.style.display !== 'none' && !el.disabled; };
function read() {
  const input = document.getElementById('chat-input');
  const activity = document.querySelector('#chat-activity-bar .chat-activity-text')?.textContent?.trim() || '';
  const running = !!(app.isProcessing || app._runControlsActive);
  const phase = running ? (app._stopPending ? 'stopped' : activity ? 'streaming' : 'sending') : input === document.activeElement ? (input?.value ? 'typing' : 'focused') : (input?.value ? 'typing' : 'idle');
  return Object.freeze({ sessionId: app.currentSessionId || '', draft: input?.value || '', gate: input?.disabled ? 'gated' : 'open', phase, sending: running && !activity, streaming: running && !!activity, stopped: !!app._stopPending, error: /error|failed/i.test(activity), controls: { send: control('chat-send'), stop: control('chat-stop-btn'), continue: control('chat-continue-btn'), attach: control('chat-attach-btn'), voice: control('chat-voice-btn'), mode: control('chat-mode-btn'), model: control('chat-model-changer'), abilities: control('chat-abilities-btn') }, attachments: document.querySelectorAll('#chat-preview-bar .attachment-preview, #chat-preview-bar [data-upload-id]').length, activity });
}
export function publishComposerState() { scheduled = false; const next = read(); if (JSON.stringify(next) === JSON.stringify(snapshot)) return; snapshot = next; listeners.forEach((listener) => listener()); }
export function scheduleComposerState() { if (!scheduled) { scheduled = true; queueMicrotask(publishComposerState); } }
export function subscribeComposerState(listener) { listeners.add(listener); return () => listeners.delete(listener); }
export function getComposerSnapshot() { return snapshot; }
export const composerActions = Object.freeze({
  setDraft(value) { const input = document.getElementById('chat-input'); if (input) { input.value = value; input.dispatchEvent(new Event('input', { bubbles: true })); } },
  send() { document.getElementById('chat-send')?.click(); }, stop() { document.getElementById('chat-stop-btn')?.click(); }, continue() { document.getElementById('chat-continue-btn')?.click(); }, attach() { document.getElementById('chat-attach-btn')?.click(); }, voice() { document.getElementById('chat-voice-btn')?.click(); }, mode() { document.getElementById('chat-mode-btn')?.click(); }, model() { document.getElementById('chat-model-changer')?.click(); }, abilities() { document.getElementById('chat-abilities-btn')?.click(); },
});
export function initComposerStore() {
  if (observer) return;
  observer = new MutationObserver(scheduleComposerState);
  observer.observe(document.getElementById('chat-input-area') || document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'hidden', 'class', 'style', 'aria-expanded'] });
  document.addEventListener('input', (event) => { if (event.target?.id === 'chat-input') scheduleComposerState(); }, true);
  document.addEventListener('focusin', scheduleComposerState); document.addEventListener('focusout', scheduleComposerState);
  publishComposerState();
}
